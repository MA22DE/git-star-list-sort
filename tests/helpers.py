"""Read-only GitHub fixtures for tests without network access."""

from __future__ import annotations

from typing import Any

from git_star_list_sort.github_api import LIST_ITEMS_QUERY, LISTS_QUERY, STARS_QUERY


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
