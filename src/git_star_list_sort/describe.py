"""Generate committed List descriptions with an OpenRouter model.

The classifier steers Jev by the text ``"<List name>: <description>"``. GitHub
Lists often carry no description at all, which leaves Jev choosing between bare
titles. This command asks an LLM to expand each title into one short description
that states what belongs in the List, then writes the result to ``lists.json``
so later classification runs need no LLM credentials of their own.

Run it by hand when the taxonomy changes:

    git-star-list-sort-describe --describe-lists-output lists.json

Hand edits to ``lists.json`` are preserved: the command refuses to overwrite an
existing description unless ``--force`` is given.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .credentials import resolve_github_token
from .github_api import GitHubAPI, paginated_lists

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_TOKENS = 100
PROMPT = (
    "You are naming the boundaries of a GitHub star List used for classifying "
    "repositories.\n"
    "Given the List title, write a description of at most two sentences and at "
    f"most {MAX_TOKENS} tokens.\n"
    "The description must state what belongs in the List and, when the title is "
    "ambiguous next to the sibling Lists, what does not.\n"
    "Reply with the description only: no preamble, no quotes, no markdown, no "
    "List title repetition.\n"
)


def load_env(path: Path) -> dict[str, str]:
    """Read a minimal KEY=VALUE ``.env`` file; missing files yield no entries."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def describe(title: str, siblings: list[str], api_key: str, model: str) -> str:
    """Return a short description for one List title."""
    siblings_text = ", ".join(name for name in siblings if name != title)
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": PROMPT},
            {
                "role": "user",
                "content": (
                    f"List title: {title}\n"
                    f"Sibling List titles: {siblings_text or '(none)'}\n"
                ),
            },
        ],
    }
    request = urllib.request.Request(
        DEFAULT_ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        hint = " Check OPENROUTER_API_KEY." if error.code == 401 else ""
        raise RuntimeError(f"OpenRouter HTTP {error.code}.{hint}") from None
    try:
        content = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ValueError("OpenRouter returned an invalid description") from None
    if not content:
        raise ValueError(f"OpenRouter returned an empty description for {title!r}")
    return " ".join(content.split())


def generate(
    lists: list[dict[str, Any]],
    api_key: str,
    model: str,
    *,
    existing: dict[str, str] | None = None,
    force: bool = False,
) -> dict[str, str]:
    """Describe each List, keeping committed descriptions unless ``force``."""
    titles = [item["name"] for item in lists]
    descriptions: dict[str, str] = dict(existing or {})
    for index, title in enumerate(titles, start=1):
        if not force and descriptions.get(title):
            print(
                f"[{index}/{len(titles)}] {title}: kept existing description",
                file=sys.stderr,
                flush=True,
            )
            continue
        text = describe(title, titles, api_key, model)
        descriptions[title] = text
        print(f"[{index}/{len(titles)}] {title}: {text}", file=sys.stderr, flush=True)
    return {title: descriptions[title] for title in titles}


def run() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--describe-lists-output",
        type=Path,
        required=True,
        help="Write generated descriptions to this JSON file",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="File holding OPENROUTER_API_KEY (default: .env)",
    )
    parser.add_argument(
        "--model",
        help=(
            "OpenRouter model id (default: OPENROUTER_MODEL from the environment "
            f"or .env, else {DEFAULT_MODEL})"
        ),
    )
    parser.add_argument(
        "--force", action="store_true", help="Regenerate existing descriptions"
    )
    args = parser.parse_args()

    env = {**load_env(args.env_file), **os.environ}
    api_key = env.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        parser.error(f"set OPENROUTER_API_KEY in the environment or in {args.env_file}")
    model = args.model or env.get("OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL
    if model != DEFAULT_MODEL and "/" not in model:
        parser.error("--model must be an OpenRouter model id such as vendor/model")

    client = GitHubAPI(resolve_github_token()[0])
    _, lists = paginated_lists(client)
    if not lists:
        raise ValueError(
            "No GitHub Lists found; create a List at https://github.com/stars"
        )

    existing = {}
    if args.describe_lists_output.exists():
        from .descriptions import load_descriptions

        existing = load_descriptions(args.describe_lists_output)

    descriptions = generate(lists, api_key, model, existing=existing, force=args.force)
    document = {
        "generated_model": model,
        "lists": descriptions,
    }
    args.describe_lists_output.parent.mkdir(parents=True, exist_ok=True)
    args.describe_lists_output.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {len(descriptions)} descriptions to {args.describe_lists_output}",
        file=sys.stderr,
    )


def main() -> None:
    try:
        run()
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
