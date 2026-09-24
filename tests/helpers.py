"""GitHub fixtures for tests without network access."""

from __future__ import annotations

from typing import Any

from git_star_list_sort.github_api import (
    ASSIGN_LIST_MUTATION,
    LIST_ITEMS_DETAIL_QUERY,
    LIST_ITEMS_QUERY,
    LISTS_QUERY,
    STARS_QUERY,
)


def repo(repository_id: str, name: str) -> dict[str, Any]:
    return {
        "id": repository_id,
        "nameWithOwner": name,
        "url": f"https://github.com/{name}",
        "description": "A useful tool",
        "isArchived": False,
        "isFork": False,
        "isPrivate": False,
        "primaryLanguage": {"name": "Python"},
        "repositoryTopics": {"nodes": [{"topic": {"name": "cli"}}]},
    }


class FakeGraphQL:
    def __init__(self, page_size: int | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.page_size = page_size
        self.lists = [
            {
                "id": "UL_tools",
                "name": "Developer Tools",
                "description": "Tools",
                "isPrivate": False,
            }
        ]
        self.star_edges = [
            {"starredAt": "2026-01-02T00:00:00Z", "node": repo("R_new", "new/tool")},
            {"starredAt": "2026-01-01T00:00:00Z", "node": repo("R_listed", "old/tool")},
        ]
        # Repository IDs that already belong to a List, keyed by List ID. Only
        # "old/tool" is listed, so the safe default classifies just that one.
        self.list_items = {"UL_tools": ["R_listed"]}

    def execute(
        self, query: str, variables: dict[str, Any], allow_partial: bool = False
    ) -> dict[str, Any]:
        self.calls.append((query, variables))
        start = int(variables["cursor"]) if variables["cursor"] else 0
        page_size = min(self.page_size or 100, variables.get("first", 100))
        end = start + page_size
        if query == LISTS_QUERY:
            return {
                "viewer": {
                    "login": "octocat",
                    "lists": {
                        "nodes": self.lists[start:end],
                        "pageInfo": {
                            "hasNextPage": end < len(self.lists),
                            "endCursor": str(end),
                        },
                    },
                }
            }
        if query == STARS_QUERY:
            return {
                "viewer": {
                    "starredRepositories": {
                        "edges": self.star_edges[start:end],
                        "pageInfo": {
                            "hasNextPage": end < len(self.star_edges),
                            "endCursor": str(end),
                        },
                        "totalCount": len(self.star_edges),
                    }
                }
            }
        if query == LIST_ITEMS_DETAIL_QUERY:
            # Served empty: enough for callers that only need the shape, since a
            # non-empty member list only affects description generation.
            repository_ids = self.list_items.get(variables["id"], [])
            return {
                "node": {
                    "items": {
                        "nodes": [{"id": item_id} for item_id in repository_ids],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        if query == LIST_ITEMS_QUERY:
            repository_ids = self.list_items.get(variables["id"], [])
            return {
                "node": {
                    "items": {
                        "nodes": [{"id": item_id} for item_id in repository_ids],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        raise AssertionError("unexpected query")


class FakeMutationGraphQL(FakeGraphQL):
    """A fake that also executes the assign mutation against in-memory state.

    Memberships are mutable sets keyed by List ID, so a confirmed apply is
    visible to later assertions and a stray mutation is impossible to miss.
    Every executed query is recorded exactly once, which lets tests count how
    often a query ran (for example the shared Lists fetch).
    """

    def __init__(self, page_size: int | None = None):
        super().__init__(page_size=page_size)
        self.members = {"UL_tools": {"R_listed"}}
        # FakeGraphQL serves LIST_ITEMS from ``list_items``; aliasing the same
        # dictionary keeps one source of truth for both the classify read path
        # and the apply write path.
        self.list_items = self.members

    def execute(
        self, query: str, variables: dict[str, Any], allow_partial: bool = False
    ) -> dict[str, Any]:
        if query == ASSIGN_LIST_MUTATION:
            self.calls.append((query, variables))
            repository_id = variables["input"]["itemId"]
            for members in self.members.values():
                members.discard(repository_id)
            for list_id in variables["input"]["listIds"]:
                self.members.setdefault(list_id, set()).add(repository_id)
            return {
                "updateUserListsForItem": {
                    "lists": [
                        {"id": list_id} for list_id in variables["input"]["listIds"]
                    ]
                }
            }
        if query == LIST_ITEMS_DETAIL_QUERY:
            self.calls.append((query, variables))
            return {
                "node": {
                    "items": {
                        "nodes": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        return super().execute(query, variables, allow_partial)

    def mutations(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (query, variables)
            for query, variables in self.calls
            if query == ASSIGN_LIST_MUTATION
        ]

    def read_queries(self) -> list[str]:
        return [query for query, _ in self.calls if query != ASSIGN_LIST_MUTATION]

    def executions(self, query: str) -> list[tuple[str, dict[str, Any]]]:
        return [(item, variables) for item, variables in self.calls if item == query]


class FakeTTY:
    """Minimal stdin stand-in whose ``isatty()`` returns True."""

    def isatty(self) -> bool:
        return True

    def readable(self) -> bool:
        return True


class FakeNotTTY(FakeTTY):
    """Minimal stdin stand-in for a pipe or redirected file (not a TTY)."""

    def isatty(self) -> bool:
        return False
