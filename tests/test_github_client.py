from __future__ import annotations

import contextlib
import io
import json
import unittest
import urllib.error
from unittest import mock

from git_star_list_sort import github_api
from tests.helpers import repo


def response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode())


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.github.com/graphql",
        status,
        "error",
        None,
        io.BytesIO(b"github-test-token"),
    )


class GitHubAPITests(unittest.TestCase):
    def test_graphql_sends_user_token_and_variables_directly(self):
        query = "query Lists($cursor: String) { viewer { login } }"
        data = {"viewer": {"login": "octocat"}}
        with mock.patch.object(
            github_api.urllib.request, "urlopen", return_value=response({"data": data})
        ) as urlopen:
            actual = github_api.GitHubAPI("github-test-token").execute(
                query, {"cursor": None}
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(data, actual)
        self.assertEqual("https://api.github.com/graphql", request.full_url)
        self.assertEqual("POST", request.method)
        self.assertEqual(
            "Bearer github-test-token", request.get_header("Authorization")
        )
        self.assertEqual(
            {"query": query, "variables": {"cursor": None}}, json.loads(request.data)
        )

    def test_readme_uses_same_user_token_and_raw_content(self):
        with mock.patch.object(
            github_api.urllib.request,
            "urlopen",
            return_value=io.BytesIO(b"# README\nText"),
        ) as urlopen:
            readme = github_api.GitHubAPI("github-test-token").readme("owner/tool")

        request = urlopen.call_args.args[0]
        self.assertEqual("# README\nText", readme)
        self.assertEqual(
            "https://api.github.com/repos/owner/tool/readme", request.full_url
        )
        self.assertEqual("GET", request.method)
        self.assertEqual(
            "Bearer github-test-token", request.get_header("Authorization")
        )
        self.assertEqual(
            "application/vnd.github.raw+json", request.get_header("Accept")
        )

    def test_stars_paginate_through_partial_graphql_errors(self):
        first = {
            "data": {
                "viewer": {
                    "starredRepositories": {
                        "edges": [{"starredAt": "2026-01-02T00:00:00Z", "node": None}],
                        "pageInfo": {"hasNextPage": True, "endCursor": "next-page"},
                    }
                }
            },
            "errors": [{"message": "Repository unavailable"}],
        }
        second = {
            "data": {
                "viewer": {
                    "starredRepositories": {
                        "edges": [
                            {
                                "starredAt": "2026-01-01T00:00:00Z",
                                "node": repo("R_tool", "owner/tool"),
                            }
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        }
        stderr = io.StringIO()
        with (
            mock.patch.object(
                github_api.urllib.request,
                "urlopen",
                side_effect=[response(first), response(second)],
            ) as urlopen,
            contextlib.redirect_stderr(stderr),
        ):
            stars = github_api.starred_repositories(
                github_api.GitHubAPI("github-test-token")
            )

        self.assertEqual(["owner/tool"], [item["name_with_owner"] for item in stars])
        self.assertEqual(
            {"cursor": "next-page", "first": 100},
            json.loads(urlopen.call_args.args[0].data)["variables"],
        )
        self.assertIn("Repository unavailable", stderr.getvalue())

    def test_graphql_errors_are_fatal_for_strict_reads_or_missing_data(self):
        for allow_partial, data in ((False, {"viewer": {}}), (True, None)):
            with (
                self.subTest(allow_partial=allow_partial, data=data),
                mock.patch.object(
                    github_api.urllib.request,
                    "urlopen",
                    return_value=response(
                        {"data": data, "errors": [{"message": "Access denied"}]}
                    ),
                ),
                self.assertRaisesRegex(RuntimeError, "Access denied"),
            ):
                github_api.GitHubAPI("github-test-token").execute(
                    "query", {}, allow_partial=allow_partial
                )

    def test_authentication_errors_do_not_retry_or_expose_the_response_body(self):
        for status in (401, 403):
            with (
                self.subTest(status=status),
                mock.patch.object(
                    github_api.urllib.request, "urlopen", side_effect=http_error(status)
                ) as urlopen,
                self.assertRaisesRegex(RuntimeError, f"HTTP {status}") as caught,
            ):
                github_api.GitHubAPI("github-test-token").execute("query", {})
            self.assertEqual(1, urlopen.call_count)
            self.assertIn("STAR_LISTS_TOKEN", str(caught.exception))
            self.assertNotIn("github-test-token", str(caught.exception))

    def test_transient_errors_retry_with_a_limit(self):
        for final in (response({"data": {"ok": True}}), http_error(504)):
            with (
                self.subTest(final=final),
                mock.patch.object(
                    github_api.urllib.request,
                    "urlopen",
                    side_effect=[http_error(502), http_error(503), final],
                ) as urlopen,
                mock.patch.object(github_api.time, "sleep") as sleep,
            ):
                client = github_api.GitHubAPI("github-test-token")
                if isinstance(final, Exception):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 504"):
                        client.execute("query", {})
                else:
                    self.assertEqual({"ok": True}, client.execute("query", {}))
            self.assertEqual(3, urlopen.call_count)
            self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_unavailable_readme_returns_none(self):
        for error in (
            http_error(404),
            http_error(403),
            urllib.error.URLError("offline"),
            TimeoutError(),
        ):
            with (
                self.subTest(error=error),
                mock.patch.object(
                    github_api.urllib.request, "urlopen", side_effect=error
                ),
            ):
                self.assertIsNone(
                    github_api.GitHubAPI("github-test-token").readme("owner/tool")
                )

    def test_rejects_invalid_graphql_responses(self):
        for payload, exception, message in (
            (b"not json", RuntimeError, "invalid JSON"),
            (b"null", TypeError, "invalid response"),
            (b'{"data":null}', TypeError, "no data"),
        ):
            with (
                self.subTest(payload=payload),
                mock.patch.object(
                    github_api.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(payload),
                ),
                self.assertRaisesRegex(exception, message),
            ):
                github_api.GitHubAPI("github-test-token").execute("query", {})

    def test_rejects_empty_or_malformed_tokens_without_echoing_them(self):
        for token in ("", " ", "github-test-token\nsecret"):
            with self.subTest(token=token), self.assertRaises(ValueError) as caught:
                github_api.GitHubAPI(token)
            self.assertNotIn("github-test-token", str(caught.exception))
