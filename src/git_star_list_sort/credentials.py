"""Resolve GitHub and Jev credentials without ever echoing their values."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

STAR_LISTS_TOKEN_ENV = "STAR_LISTS_TOKEN"
GH_TOKEN_ENV = "GH_TOKEN"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
JEV_API_KEY_ENV = "JEV_API_KEY"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"

GITHUB_TOKEN_ENVS = (STAR_LISTS_TOKEN_ENV, GITHUB_TOKEN_ENV, GH_TOKEN_ENV)
_GH_HOSTNAME = "github.com"
ENV_FILE_NAME = ".env"
APP_NAME = "git-star-list-sort"
NO_DOTENV_ENV = "GIT_STAR_LIST_SORT_NO_DOTENV"
_GH_TOKEN_SOURCE = "gh auth token"
_ACCOUNT_MARKER_PARTS = ("Logged", "in", "to")
_ACCOUNT_KEYWORD = "account"
# Placeholder for a login line whose account name could not be read. Keeping it
# makes the multi-account guard fail closed on unrecognised `gh` output.
_UNPARSEABLE_ACCOUNT = "\x00unparseable"

MISSING_GITHUB_TOKEN = (
    f"set {STAR_LISTS_TOKEN_ENV} (or {GH_TOKEN_ENV}/{GITHUB_TOKEN_ENV}), or log in "
    f"with `gh auth login --hostname {_GH_HOSTNAME}` so that "
    f"`gh auth token --hostname {_GH_HOSTNAME}` returns a token"
)
MISSING_JEV_KEY = f"set {JEV_API_KEY_ENV} or {TYPESAFE_API_KEY_ENV}"
GH_FAILED = (
    f"Could not read a GitHub token from {_GH_TOKEN_SOURCE}. {MISSING_GITHUB_TOKEN}"
)


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _first_environment_value(
    environ: Mapping[str, str], names: tuple[str, ...]
) -> tuple[str, str] | None:
    for name in names:
        value = _clean(environ.get(name))
        if value:
            return value, name
    return None


def _run(run: Callable[..., object] | None) -> Callable[..., object]:
    return run if run is not None else subprocess.run


def _capture(run: Callable[..., object], *command: str) -> object:
    try:
        return run(list(command), capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(GH_FAILED) from None


def _output(result: object) -> str:
    return getattr(result, "stdout", None) or ""


def _execute(run: Callable[..., object], *command: str) -> str:
    result = _capture(run, *command)
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(GH_FAILED)
    return _output(result)


def _logged_in_accounts(status: str) -> list[str]:
    """Account logins named in `gh auth status` output.

    `gh` prints one line per account, e.g.
    `  ✓ Logged in to github.com account octocat (keyring)`. The checkbox and any
    other decoration is ignored, and matching is done on the token sequence
    "Logged in to" rather than a single whitespace token, because the marker
    itself contains spaces.
    """
    accounts = []
    marker = len(_ACCOUNT_MARKER_PARTS)
    for line in status.splitlines():
        parts = line.split()
        for start in range(len(parts) - marker + 1):
            if tuple(parts[start : start + marker]) != _ACCOUNT_MARKER_PARTS:
                continue
            try:
                login = parts[parts.index(_ACCOUNT_KEYWORD, start) + 1]
            except (ValueError, IndexError):
                # The line announces a login we could not read. Recording a
                # placeholder keeps the guard fail-closed instead of letting an
                # unrecognised format look like "no account".
                accounts.append(_UNPARSEABLE_ACCOUNT)
                break
            accounts.append(login)
            break
    return accounts


def _gh_token(run: Callable[..., object]) -> str:
    accounts = _logged_in_accounts(
        _execute(run, "gh", "auth", "status", "--hostname", _GH_HOSTNAME)
    )
    # Count LOGIN LINES, not distinct logins: two unreadable logins must still
    # refuse, and a genuinely duplicated block must not. Deduplicating by value
    # would collapse distinct-but-unparseable accounts into one.
    if len(accounts) > 1:
        named = ", ".join(
            "an unrecognised account" if item == _UNPARSEABLE_ACCOUNT else item
            for item in accounts
        )
        raise RuntimeError(
            f"`gh` is logged in to more than one account "
            f"({named}); refusing to guess which one to "
            f"use, so set {STAR_LISTS_TOKEN_ENV} to the intended token explicitly"
        )
    try:
        result = run(
            ["gh", "auth", "token", "--hostname", _GH_HOSTNAME],
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError(GH_FAILED) from None
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(GH_FAILED)
    token = _output(result).strip()
    if not token:
        raise RuntimeError(f"{_GH_TOKEN_SOURCE} returned no token. {GH_FAILED}")
    return token


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` file, ignoring comments and blanks."""
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


def config_candidates(start: Path | None = None) -> list[Path]:
    """Where to look for credentials, most specific first.

    A project-local ``.env`` wins, so a checkout can carry its own settings; the
    user config directory follows, so the installed CLI works from any working
    directory.
    """
    directory = (start or Path.cwd()).resolve()
    candidates = [
        candidate / ENV_FILE_NAME for candidate in [directory, *directory.parents]
    ]
    candidates.append(Path.home() / ".config" / APP_NAME / ENV_FILE_NAME)
    return candidates


def dotenv_enabled() -> bool:
    """Whether ``.env`` files may supply credentials.

    Set ``GIT_STAR_LIST_SORT_NO_DOTENV=1`` to force pure-environment resolution,
    which is what the test suite and any scripted run needs for reproducibility.
    """
    return not os.environ.get(NO_DOTENV_ENV, "").strip()


def dotenv_values(start: Path | None = None) -> dict[str, str]:
    """Read the first available ``.env`` from the candidate locations.

    An installed console script cannot source a shell profile, so without this a
    token in ``.env`` is invisible and ``apply`` fails for a reason that looks
    unrelated to configuration. Real environment variables still win.
    """
    if not dotenv_enabled():
        return {}
    for path in config_candidates(start):
        if path.is_file():
            return load_env_file(path)
    return {}


def resolve_github_token(
    env: Mapping[str, str] | None = None,
    *,
    run: Callable[..., object] | None = None,
    include_dotenv: bool = True,
) -> tuple[str, str]:
    """Return the GitHub token and a label naming its source.

    Precedence: environment variables, then ``.env``, then the `gh` CLI.
    """
    environ = dict(os.environ if env is None else env)
    if include_dotenv and env is None and dotenv_enabled():
        for name, value in dotenv_values().items():
            environ.setdefault(name, value)
    found = _first_environment_value(environ, GITHUB_TOKEN_ENVS)
    if found is not None:
        return found
    return _gh_token(_run(run)), _GH_TOKEN_SOURCE


def resolve_jev_credentials(
    env: Mapping[str, str] | None = None, *, include_dotenv: bool = True
) -> tuple[str, str]:
    """Return the Jev API key and a label naming its source."""
    environ = dict(os.environ if env is None else env)
    if include_dotenv and env is None and dotenv_enabled():
        for name, value in dotenv_values().items():
            environ.setdefault(name, value)
    found = _first_environment_value(environ, (JEV_API_KEY_ENV, TYPESAFE_API_KEY_ENV))
    if found is None:
        raise RuntimeError(MISSING_JEV_KEY)
    return found
