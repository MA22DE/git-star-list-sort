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

from .credentials import dotenv_values, load_env_file, resolve_github_token
from .descriptions import load_descriptions
from .github_api import (
    GitHubAPI,
    GraphQLExecutor,
    format_evidence,
    list_item_details,
    paginated_lists,
)

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
MAX_TOKENS = 100
# This model family emits chain-of-thought tokens before the answer, and a cap
# sized for the visible answer alone is spent entirely on reasoning: the response
# comes back with `content: null` and `finish_reason: "length"`. The request
# budget therefore covers reasoning plus answer, while the prompt keeps the
# visible description within the length you asked for.
MAX_SENTENCES = 2
MAX_WORDS = 60
REQUEST_TOKEN_BUDGET = 2000
# Reasoning length varies by title; a short one succeeds at 800 while an abstract
# title has been observed to need ~860. Retrying once at double the budget turns
# a single hard title into a slower success rather than an aborted batch.
MAX_TOKEN_BUDGET = 4000
PROMPT = (
    "You are writing the description of a GitHub star List, which is used as the "
    "criteria when classifying other repositories into Lists.\n"
    f"Write at most {MAX_SENTENCES} sentences and at most {MAX_WORDS} words.\n"
    "State what belongs in this List, and name the sibling Lists it is most "
    "likely to be confused with and what does not belong here.\n"
    "Ground the description in the evidence provided: the repositories already "
    "in the List, their topics, and their languages. Do not invent a theme that "
    "the evidence does not support, and do not assume a List is about a general "
    "topic just because its title sounds like one.\n"
    "Reply with the description only: no preamble, no quotes, no markdown, no "
    "repetition of the List title.\n"
)


def load_env(path: Path | None) -> dict[str, str]:
    """OpenRouter settings from an explicit file, else the shared discovery.

    Passing no ``--env-file`` falls back to the same ``.env`` search the other
    commands use (nearest file walking up, then ``~/.config``), so the key does
    not have to be exported or duplicated into the working directory.
    """
    if path is not None:
        return load_env_file(path)
    return dotenv_values()


def _request_description(
    title: str,
    siblings_text: str,
    api_key: str,
    model: str,
    budget: int,
    evidence: str = "",
) -> tuple[str | None, str | None]:
    """Ask for one description.

    Returns ``(description, None)`` on success or ``(None, reason)`` when the
    model produced no visible text, so the caller can decide whether a larger
    budget is worth trying.
    """
    payload = {
        "model": model,
        "max_tokens": budget,
        "messages": [
            {"role": "system", "content": PROMPT},
            {
                "role": "user",
                "content": (
                    f"List title: {title}\n"
                    f"Sibling List titles: {siblings_text or '(none)'}\n"
                    + (f"\n{evidence}" if evidence else "")
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
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        error.close()
        hint = " Check OPENROUTER_API_KEY." if error.code == 401 else ""
        raise RuntimeError(f"OpenRouter HTTP {error.code}.{hint}") from None
    try:
        choice = result["choices"][0]
        message = choice["message"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("OpenRouter returned an invalid response") from None
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return " ".join(content.split()), None
    finish = choice.get("finish_reason")
    # A reasoning model that runs out of budget returns null content, which is
    # otherwise indistinguishable from a malformed response.
    reason = (
        f"the {budget}-token budget was exhausted by the model's reasoning"
        if finish == "length"
        else f"finish_reason={finish!r}"
    )
    return None, reason


def describe(
    title: str,
    siblings: list[str],
    api_key: str,
    model: str,
    evidence: str = "",
) -> str:
    """Return a short description for one List title.

    Reasoning length varies by title: a concrete name like ``SQLite`` answers
    within a small budget, while an abstract one can burn several hundred tokens
    of reasoning first. Because a batch should not fail on a single hard title,
    an exhausted budget is retried once at double the size.
    """
    siblings_text = ", ".join(name for name in siblings if name != title)
    budget = REQUEST_TOKEN_BUDGET
    reason = "no response"
    while budget <= MAX_TOKEN_BUDGET:
        description, reason = _request_description(
            title, siblings_text, api_key, model, budget, evidence
        )
        if description is not None:
            return description
        if "exhausted" not in reason or budget * 2 > MAX_TOKEN_BUDGET:
            break
        budget *= 2
    raise ValueError(
        f"OpenRouter returned no description for {title!r}: {reason}. "
        f"Try a larger budget than {MAX_TOKEN_BUDGET} tokens."
    )


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


def generate_partial(
    lists: list[dict[str, Any]],
    api_key: str,
    model: str,
    *,
    existing: dict[str, str] | None = None,
    force: bool = False,
    evidence: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """Describe each List, returning successes plus the titles that failed.

    One uncooperative title must not discard the descriptions already generated.
    ``evidence`` maps a List name to the rendered details of its members, which
    keeps a vague title from being described as a general topic.
    """
    titles = [item["name"] for item in lists]
    descriptions: dict[str, str] = dict(existing or {})
    failures: list[tuple[str, str]] = []
    for index, title in enumerate(titles, start=1):
        if not force and descriptions.get(title):
            print(
                f"[{index}/{len(titles)}] {title}: kept existing description",
                file=sys.stderr,
                flush=True,
            )
            continue
        try:
            text = describe(
                title, titles, api_key, model, (evidence or {}).get(title, "")
            )
        except (RuntimeError, ValueError) as error:
            failures.append((title, str(error)))
            print(f"[{index}/{len(titles)}] {title}: FAILED - {error}", file=sys.stderr)
            continue
        descriptions[title] = text
        print(f"[{index}/{len(titles)}] {title}: {text}", file=sys.stderr, flush=True)
    ordered = {title: descriptions[title] for title in titles if title in descriptions}
    return ordered, failures


def write_descriptions(
    lists_file: Path,
    model: str,
    *,
    existing: dict[str, str],
    generated: dict[str, str],
) -> dict[str, str]:
    """Merge and write the descriptions document, then read it back.

    The single serializer for ``lists.json``: ``generated`` holds only the Lists
    that exist on GitHub right now, so merging it over ``existing`` is what keeps
    a deleted List's committed description, exactly as the callers' "no longer on
    GitHub" note promises. The file is rewritten only when the content actually
    changes, and the caller always gets back what the file holds.
    """
    document = {"generated_model": model, "lists": {**existing, **generated}}
    if lists_file.is_file():
        try:
            current = json.loads(lists_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = None
        if current == document:
            return load_descriptions(lists_file)
    lists_file.parent.mkdir(parents=True, exist_ok=True)
    lists_file.write_text(
        json.dumps(document, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return load_descriptions(lists_file)


def fill_missing_descriptions(
    client: GraphQLExecutor,
    lists: list[dict[str, Any]],
    *,
    lists_file: Path,
    force: bool = False,
) -> tuple[dict[str, str] | None, list[str]]:
    """Describe Lists that lack a committed description, in place.

    Returns ``(descriptions_to_use, warnings)``. Descriptions are always read
    back from ``lists_file`` so the sort uses exactly what was written, and the
    file stays the single source of truth. ``None`` means the caller should fall
    back to live GitHub descriptions (or bare names), which happens when there is
    nothing to do, or when the OpenRouter key is absent or the call failed: a
    sort must not become impossible because the description generator is
    unavailable.
    """
    existing = load_descriptions(lists_file)
    live_names = [item["name"] for item in lists]
    missing = [name for name in live_names if not existing.get(name)]
    disappeared = sorted(set(existing) - set(live_names))
    warnings: list[str] = []
    for name in disappeared:
        warnings.append(
            f"List no longer on GitHub: {name} (its committed description is kept)"
        )
    if not missing and not force:
        # Nothing to generate, but the committed descriptions are still the ones
        # to classify with (they outrank the live GitHub description).
        return existing, warnings
    env = dotenv_values()
    api_key = (
        os.environ.get("OPENROUTER_API_KEY", "").strip()
        or env.get("OPENROUTER_API_KEY", "").strip()
    )
    if not api_key:
        return None, warnings + [
            f"{len(missing)} List(s) have no committed descriptions: "
            + ", ".join(missing)
            + ". Set OPENROUTER_API_KEY to generate descriptions for them."
        ]
    model = (
        os.environ.get("OPENROUTER_MODEL", "").strip()
        or env.get("OPENROUTER_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    evidence: dict[str, str] = {}
    targets = [item for item in lists if item["name"] in set(missing)]
    for item in targets:
        details = list_item_details(client, item["id"])
        evidence[item["name"]] = format_evidence(details)
    try:
        generated, failures = generate_partial(
            lists,
            api_key,
            model,
            existing=existing,
            force=force,
            evidence=evidence,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return None, warnings + [f"could not generate descriptions: {error}"]
    for title, reason in failures:
        warnings.append(f"no description for {title!r}: {reason}")
    return write_descriptions(
        lists_file, model, existing=existing, generated=generated
    ), warnings


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
        help=(
            "Explicit file holding OPENROUTER_API_KEY; by default the same .env "
            "search as the other commands is used (nearest file, then ~/.config)"
        ),
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
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report Lists that are missing a description (including Lists added "
            "since the file was written) and generate only those; needs no key "
            "to report"
        ),
    )
    args = parser.parse_args()

    env = {**load_env(args.env_file), **os.environ}
    api_key = env.get("OPENROUTER_API_KEY", "").strip()
    if not api_key and not args.check:
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

    # Which Lists actually lack a committed description? New Lists appear as their
    # titles change, so this is the routine maintenance question.
    live_names = [item["name"] for item in lists]
    missing = [name for name in live_names if not existing.get(name)]
    disappeared = sorted(set(existing) - set(live_names))

    if args.check:
        for name in missing:
            print(f"  needs description: {name}", file=sys.stderr)
        for name in disappeared:
            print(
                f"  no longer a List on GitHub: {name} (kept in the file)",
                file=sys.stderr,
            )
        print(
            f"{len(live_names)} Lists, {len(live_names) - len(missing)} described, "
            f"{len(missing)} missing" + ("; nothing to do" if not missing else ""),
            file=sys.stderr,
        )
        if not missing:
            return
        # Without OPENROUTER_API_KEY the check is still useful as a report.
        if not api_key:
            print(
                "Set OPENROUTER_API_KEY to generate the missing descriptions.",
                file=sys.stderr,
            )
            sys.exit(1)

    if not missing and not args.force:
        print(
            f"All {len(live_names)} Lists already have descriptions; nothing to do.",
            file=sys.stderr,
        )
        return

    targets = [item for item in lists if item["name"] in set(missing)]
    evidence: dict[str, str] = {}
    for index, item in enumerate(targets, start=1):
        details = list_item_details(client, item["id"])
        evidence[item["name"]] = format_evidence(details)
        note = "empty" if not details else f"{len(details)} member(s)"
        print(
            f"[{index}/{len(targets)}] evidence for {item['name']}: {note}",
            file=sys.stderr,
            flush=True,
        )

    descriptions, failures = generate_partial(
        lists,
        api_key,
        model,
        existing=existing,
        force=args.force,
        evidence=evidence,
    )
    # Keep descriptions for Lists that are no longer on GitHub: the check above
    # reports them as kept rather than deleted.
    write_descriptions(
        args.describe_lists_output,
        model,
        existing=existing,
        generated=descriptions,
    )
    print(
        f"Wrote {len(descriptions)} of {len(lists)} descriptions to "
        f"{args.describe_lists_output}",
        file=sys.stderr,
    )
    if failures:
        # Saved output is still usable; list what is missing and why.
        for title, reason in failures:
            print(f"No description for {title!r}: {reason}", file=sys.stderr)
        sys.exit(1)


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
