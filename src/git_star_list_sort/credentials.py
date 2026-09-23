"""Resolve GitHub and Jev credentials without ever echoing their values."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping

STAR_LISTS_TOKEN_ENV = "STAR_LISTS_TOKEN"
GH_TOKEN_ENV = "GH_TOKEN"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"
JEV_API_KEY_ENV = "JEV_API_KEY"
TYPESAFE_API_KEY_ENV = "TYPESAFE_API_KEY"

GITHUB_TOKEN_ENVS = (STAR_LISTS_TOKEN_ENV, GITHUB_TOKEN_ENV, GH_TOKEN_ENV)
_GH_HOSTNAME = "github.com"
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


def resolve_github_token(
    env: Mapping[str, str] | None = None, *, run: Callable[..., object] | None = None
) -> tuple[str, str]:
    """Return the GitHub token and a human-readable label naming its source."""
    environ = os.environ if env is None else env
    found = _first_environment_value(environ, GITHUB_TOKEN_ENVS)
    if found is not None:
        return found
    return _gh_token(_run(run)), _GH_TOKEN_SOURCE


def resolve_jev_credentials(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    """Return the Jev API key and a human-readable label naming its source."""
    environ = os.environ if env is None else env
    found = _first_environment_value(environ, (JEV_API_KEY_ENV, TYPESAFE_API_KEY_ENV))
    if found is None:
        raise RuntimeError(MISSING_JEV_KEY)
    return found
