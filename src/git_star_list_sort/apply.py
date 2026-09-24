"""Apply a classification report to existing GitHub Lists."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .credentials import resolve_github_token
from .github_api import (
    ASSIGN_LIST_MUTATION,
    GitHubAPI,
    GraphQLExecutor,
    list_memberships,
    paginated_lists,
)


def validate_report(report: Any) -> tuple[str, dict[str, str | None]]:
    if not isinstance(report, dict):
        raise TypeError("report must be an object")
    login = report.get("viewer_login")
    if not isinstance(login, str) or not login:
        raise ValueError("report viewer_login is required")
    lists = report.get("existing_lists")
    results = report.get("results")
    if not isinstance(lists, list) or not isinstance(results, list):
        raise TypeError("report existing_lists and results must be arrays")
    list_ids = set()
    for item in lists:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not item["id"]
        ):
            raise ValueError("report Lists must have nonempty IDs")
        list_ids.add(item["id"])

    assignments: dict[str, str | None] = {}
    for result in results:
        if not isinstance(result, dict):
            raise TypeError("report results must be objects")
        repository = result.get("repository")
        if (
            not isinstance(repository, dict)
            or not isinstance(repository.get("id"), str)
            or not repository["id"]
        ):
            raise ValueError("report repositories must have nonempty IDs")
        repository_id = repository["id"]
        if repository_id in assignments:
            raise ValueError(f"duplicate repository in report: {repository_id}")
        if "list_id" not in result:
            raise ValueError(
                "each report result must have list_id, or null for no match"
            )
        list_id = result["list_id"]
        if list_id is not None and (
            not isinstance(list_id, str) or list_id not in list_ids
        ):
            raise ValueError(f"unknown List in report: {list_id!r}")
        assignments[repository_id] = list_id
    return login, assignments


def apply_report(
    client: GraphQLExecutor, report: Any, *, dry_run: bool = False
) -> dict[str, int]:
    report_login, assignments = validate_report(report)
    login, current_lists = paginated_lists(client)
    if login.casefold() != report_login.casefold():
        raise ValueError("STAR_LISTS_TOKEN belongs to a different user than the report")
    current_ids = {item["id"] for item in current_lists}
    targets = {list_id for list_id in assignments.values() if list_id is not None}
    missing = targets - current_ids
    if missing:
        raise ValueError(f"Lists no longer exist: {sorted(missing)}")

    memberships: dict[str, set[str]] = {}
    list_names = {item["id"]: item["name"] for item in current_lists}
    per_list: Counter[str] = Counter()
    if targets:
        for list_id, repository_ids in list_memberships(
            client, sorted(current_ids)
        ).items():
            for repository_id in repository_ids:
                memberships.setdefault(repository_id, set()).add(list_id)

    changed = 0
    already_assigned = 0
    unmatched = 0
    for repository_id, list_id in assignments.items():
        if list_id is None:
            unmatched += 1
            continue
        existing = memberships.get(repository_id, set())
        if list_id in existing:
            already_assigned += 1
            continue
        if not dry_run:
            requested = existing | {list_id}
            result = client.execute(
                ASSIGN_LIST_MUTATION,
                {
                    "input": {
                        "itemId": repository_id,
                        "listIds": sorted(requested),
                    }
                },
            )
            confirmed = {
                item["id"] for item in result["updateUserListsForItem"]["lists"]
            }
            if not requested <= confirmed:
                raise RuntimeError(
                    f"GitHub did not confirm all List assignments for {repository_id}"
                )
        changed += 1
        if list_id in list_names:
            per_list[list_names[list_id]] += 1
    return {
        "would_apply" if dry_run else "applied": changed,
        "already_assigned": already_assigned,
        "no_matching_category": unmatched,
        "per_list": dict(per_list),
    }


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate against GitHub without updating Lists",
    )
    args = parser.parse_args()
    try:
        token, source = resolve_github_token()
    except (RuntimeError, ValueError) as error:
        parser.error(str(error))
    print(f"GitHub credentials: {source}", file=sys.stderr)
    with args.report.open(encoding="utf-8") as handle:
        report = json.load(handle)
    summary = apply_report(GitHubAPI(token), report, dry_run=args.dry_run)
    print(json.dumps(summary, sort_keys=True))


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
