"""GitHub API client, queries, and pagination for star classification."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

RETRYABLE_STATUS = {429, 502, 503, 504}


def _has_required_scopes(errors: object) -> bool:
    """True when no GraphQL error reports a missing OAuth scope."""
    if not isinstance(errors, list):
        return True
    for error in errors:
        if isinstance(error, dict) and error.get("type") == "INSUFFICIENT_SCOPES":
            return False
    return True


class GitHubAPI:
    """Access GitHub GraphQL and REST endpoints using a user token."""

    def __init__(self, token: str):
        self.token = token.strip()
        if not self.token:
            raise ValueError("set a GitHub token (STAR_LISTS_TOKEN)")
        if any(character.isspace() for character in self.token):
            raise ValueError("the GitHub token must not contain whitespace")

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        accept: str = "application/vnd.github+json",
        timeout: int = 60,
    ) -> bytes:
        request = urllib.request.Request(
            f"https://api.github.com/{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": accept,
                "Content-Type": "application/json",
                "User-Agent": "github-star-organizer-jev",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            method="POST" if payload is not None else "GET",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read()
            except urllib.error.HTTPError as error:
                error.close()
                if error.code not in RETRYABLE_STATUS or attempt == 2:
                    raise
                time.sleep(2**attempt)
        raise AssertionError("unreachable")

    def execute(
        self, query: str, variables: dict[str, Any], allow_partial: bool = False
    ) -> dict[str, Any]:
        try:
            response = self._request(
                "graphql", {"query": query, "variables": variables}
            )
        except urllib.error.HTTPError as error:
            hint = (
                " Check the GitHub token and its permissions."
                if error.code in {401, 403}
                else ""
            )
            raise RuntimeError(f"GitHub GraphQL HTTP {error.code}.{hint}") from None
        try:
            result = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RuntimeError("GitHub GraphQL returned invalid JSON") from None
        if not isinstance(result, dict):
            raise TypeError("GitHub GraphQL returned an invalid response")
        data = result.get("data")
        if result.get("errors"):
            # A missing `user` scope is the common failure when applying, and the
            # raw GraphQL text does not say what to do about it.
            if not _has_required_scopes(result["errors"]):
                raise RuntimeError(
                    "the GitHub token cannot modify Lists: the `user` scope is "
                    "required. Create a classic PAT with that scope and set "
                    "STAR_LISTS_TOKEN to it (a `gh auth token` is enough to "
                    "read, not to apply)."
                )
            if not allow_partial or not isinstance(data, dict):
                raise RuntimeError(f"GitHub GraphQL errors: {result['errors']}")
            print(
                f"GitHub GraphQL partial errors: {result['errors']}",
                file=sys.stderr,
                flush=True,
            )
        if not isinstance(data, dict):
            raise TypeError("GitHub GraphQL returned no data")
        return data

    def readme(self, name_with_owner: str) -> str | None:
        path = f"repos/{urllib.parse.quote(name_with_owner, safe='/')}/readme"
        try:
            return self._request(
                path, accept="application/vnd.github.raw+json", timeout=30
            ).decode("utf-8", errors="replace")
        except OSError:
            return None


LISTS_QUERY = """
query Lists($cursor: String) {
  viewer {
    login
    lists(first: 100, after: $cursor) {
      nodes { id name description isPrivate }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""


STARS_QUERY = """
query Stars($cursor: String, $first: Int = 100) {
  viewer {
    starredRepositories(
      first: $first
      after: $cursor
      orderBy: {field: STARRED_AT, direction: DESC}
    ) {
      edges {
        starredAt
        node {
          id
          nameWithOwner
          url
          description
          isArchived
          isFork
          isPrivate
          primaryLanguage { name }
          repositoryTopics(first: 20) { nodes { topic { name } } }
        }
      }
      pageInfo { hasNextPage endCursor }
      totalCount
    }
  }
}
"""


class GraphQLExecutor(Protocol):
    def execute(
        self, query: str, variables: dict[str, Any], allow_partial: bool = False
    ) -> dict[str, Any]: ...


LIST_ITEMS_QUERY = """
query ListItems($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on UserList {
      items(first: 100, after: $cursor) {
        nodes { ... on Repository { id } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

ASSIGN_LIST_MUTATION = """
mutation AssignList($input: UpdateUserListsForItemInput!) {
  updateUserListsForItem(input: $input) { lists { id name } }
}
"""


LIST_ITEMS_DETAIL_QUERY = """
query ListItemsDetail($id: ID!, $cursor: String) {
  node(id: $id) {
    ... on UserList {
      items(first: 50, after: $cursor) {
        nodes {
          ... on Repository {
            nameWithOwner
            description
            primaryLanguage { name }
            repositoryTopics(first: 8) { nodes { topic { name } } }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


def list_item_details(
    client: GraphQLExecutor, list_id: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Repositories already in a List, with the fields that describe them.

    A List's title alone often under-determines what belongs in it, so the
    description generator uses the actual members as evidence.
    """
    details: list[dict[str, Any]] = []
    cursor = None
    while len(details) < limit:
        node = client.execute(
            LIST_ITEMS_DETAIL_QUERY, {"id": list_id, "cursor": cursor}
        )["node"]
        if node is None:
            return details
        connection = node["items"]
        for item in connection["nodes"]:
            if not isinstance(item, dict):
                continue
            details.append(
                {
                    "name_with_owner": item.get("nameWithOwner"),
                    "description": item.get("description"),
                    "language": (
                        item["primaryLanguage"]["name"]
                        if item.get("primaryLanguage")
                        else None
                    ),
                    "topics": [
                        topic["topic"]["name"]
                        for topic in (item.get("repositoryTopics") or {}).get(
                            "nodes", []
                        )
                        if isinstance(topic, dict) and topic.get("topic")
                    ],
                }
            )
            if len(details) >= limit:
                break
        if not connection["pageInfo"]["hasNextPage"]:
            break
        cursor = connection["pageInfo"]["endCursor"]
    return details


def format_evidence(details: list[dict[str, Any]]) -> str:
    """Render List members as prompt evidence."""
    if not details:
        return (
            "Evidence: this List is currently empty, so its title and the "
            "distinction from sibling Lists are the only available signal."
        )
    lines = ["Evidence - repositories already in this List:"]
    for item in details:
        parts = [f"- {item['name_with_owner']}"]
        if item.get("description"):
            parts.append(f": {item['description'].strip()}")
        if item.get("language"):
            parts.append(f" [{item['language']}]")
        if item.get("topics"):
            parts.append(f" topics: {', '.join(item['topics'])}")
        lines.append("".join(parts))
    return "\n".join(lines)


def list_memberships(
    client: GraphQLExecutor, list_ids: list[str]
) -> dict[str, set[str]]:
    memberships: dict[str, set[str]] = {}
    for list_id in list_ids:
        cursor = None
        memberships[list_id] = set()
        while True:
            # Partial membership data could remove an existing assignment.
            node = client.execute(LIST_ITEMS_QUERY, {"id": list_id, "cursor": cursor})[
                "node"
            ]
            if node is None:
                raise ValueError(f"GitHub List is unavailable: {list_id}")
            connection = node["items"]
            for item in connection["nodes"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise TypeError(f"Cannot read all memberships for List: {list_id}")
                memberships[list_id].add(item["id"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
    return memberships


def paginated_lists(client: GraphQLExecutor) -> tuple[str, list[dict[str, Any]]]:
    cursor = None
    login = ""
    lists: list[dict[str, Any]] = []
    while True:
        viewer = client.execute(LISTS_QUERY, {"cursor": cursor})["viewer"]
        login = viewer["login"]
        connection = viewer["lists"]
        lists.extend(connection["nodes"])
        if not connection["pageInfo"]["hasNextPage"]:
            return login, lists
        cursor = connection["pageInfo"]["endCursor"]


def starred_repositories(
    client: GraphQLExecutor,
    cutoff: datetime | None = None,
    *,
    limit: int | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    if limit is not None and limit < 1:
        raise ValueError("limit must be positive or None")
    cursor = None
    repositories: list[dict[str, Any]] = []
    while True:
        page_size = min(100, limit - len(repositories)) if limit else 100
        connection = client.execute(
            STARS_QUERY, {"cursor": cursor, "first": page_size}, allow_partial=True
        )["viewer"]["starredRepositories"]
        reached_cutoff = False
        for edge in connection["edges"]:
            starred_at = datetime.fromisoformat(edge["starredAt"])
            if cutoff is not None and starred_at < cutoff:
                reached_cutoff = True
                break
            repository = edge["node"]
            if repository is None:
                continue
            repositories.append(
                {
                    "id": repository["id"],
                    "name_with_owner": repository["nameWithOwner"],
                    "url": repository["url"],
                    "description": repository["description"],
                    "language": (
                        repository["primaryLanguage"]["name"]
                        if repository["primaryLanguage"]
                        else None
                    ),
                    "topics": [
                        item["topic"]["name"]
                        for item in repository["repositoryTopics"]["nodes"]
                    ],
                    "archived": repository["isArchived"],
                    "fork": repository["isFork"],
                    "private": repository["isPrivate"],
                    "starred_at": edge["starredAt"],
                }
            )
            if limit is not None and len(repositories) >= limit:
                break
        if on_progress:
            on_progress(len(repositories), connection["totalCount"])
        if (
            reached_cutoff
            or (limit is not None and len(repositories) >= limit)
            or not connection["pageInfo"]["hasNextPage"]
        ):
            return repositories
        cursor = connection["pageInfo"]["endCursor"]
