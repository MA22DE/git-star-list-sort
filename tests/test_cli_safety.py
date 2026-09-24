"""CLI credential resolution and report-only default safety."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_star_list_sort import cli
from git_star_list_sort.github_api import (
    ASSIGN_LIST_MUTATION,
    LIST_ITEMS_QUERY,
    STARS_QUERY,
)
from tests.helpers import FakeGraphQL, repo


def credentials_module():
    """Import the new credentials module lazily so CLI failures stay visible."""
    from git_star_list_sort import credentials

    return credentials


def credential_names() -> dict[str, str]:
    """Environment variable names, tolerating the module until it exists."""
    try:
        credentials = credentials_module()
    except ImportError:
        return {
            "star_lists": "STAR_LISTS_TOKEN",
            "jev": "JEV_API_KEY",
            "gh": "GH_TOKEN",
            "github": "GITHUB_TOKEN",
        }
    return {
        "star_lists": credentials.STAR_LISTS_TOKEN_ENV,
        "jev": credentials.JEV_API_KEY_ENV,
        "gh": credentials.GH_TOKEN_ENV,
        "github": credentials.GITHUB_TOKEN_ENV,
    }


class FakeOrganizationAPI(FakeGraphQL):
    """Read-only GraphQL fake that also serves List membership queries."""

    def __init__(self, page_size: int | None = None):
        super().__init__(page_size=page_size)
        self.star_edges = [
            {"starredAt": "2026-01-02T00:00:00Z", "node": repo("R_new", "new/tool")},
            {"starredAt": "2026-01-01T00:00:00Z", "node": repo("R_listed", "old/tool")},
        ]
        self.members = {"UL_tools": {"R_listed"}}

    def execute(self, query, variables, allow_partial=False):
        if query == LIST_ITEMS_QUERY:
            self.calls.append((query, variables))
            ids = sorted(self.members[variables["id"]])
            start = int(variables["cursor"]) if variables["cursor"] else 0
            end = start + (self.page_size or 100)
            return {
                "node": {
                    "items": {
                        "nodes": [{"id": item} for item in ids[start:end]],
                        "pageInfo": {
                            "hasNextPage": end < len(ids),
                            "endCursor": str(end),
                        },
                    }
                }
            }
        return super().execute(query, variables, allow_partial)

    def mutations(self):
        return [
            (query, variables)
            for query, variables in self.calls
            if query == ASSIGN_LIST_MUTATION
        ]

    def read_queries(self):
        return [query for query, _ in self.calls if query != ASSIGN_LIST_MUTATION]


def classification(list_id="UL_tools", probabilities=None):
    def classify(repository, criteria, api_key, model, endpoint=None):
        return {
            "list_id": list_id,
            "confidence": 0.8,
            "probabilities": probabilities or {"UL_tools": 0.9},
            "model": model,
            "usage": {},
            "elapsed_seconds": 0.01,
        }

    return classify


def hermetic(environment: dict) -> dict:
    """Environment for a hermetic run.

    ``mock.patch.dict(..., clear=True)`` wipes the opt-out flag that stops the
    credential resolvers reading a developer's ``.env`` or ``~/.config`` file, so
    it must be re-added here or these tests would silently use real credentials.
    """
    return {
        # Blanked before any override: a developer's real key must never make a
        # test generate descriptions (and write files) through the network.
        "OPENROUTER_API_KEY": "",
        **environment,
        "GIT_STAR_LIST_SORT_NO_DOTENV": "1",
        # Point the lists file at a path that does not exist so tests never
        # read the developer's real checkout lists.json, which drifts with
        # live GitHub and would make assertions depend on account state.
        "STAR_LISTS_FILE": str(Path(tempfile.gettempdir()) / "no-such-lists.json"),
    }


def run_cli(argv, client, classify=None, env=None):
    """Run the CLI with hermetic environment, API, and Jev stubs."""
    names = credential_names()
    environment = {
        names["star_lists"]: "github-test-token",
        names["jev"]: "jev-test-key",
        names["gh"]: "",
        names["github"]: "",
        **(env or {}),
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    classifier = classify or classification()
    with (
        mock.patch.dict(os.environ, hermetic(environment), clear=True),
        mock.patch.object(sys, "argv", ["git-star-list-sort", *argv]),
        mock.patch.object(cli, "GitHubAPI", return_value=client),
        mock.patch.object(cli, "fetch_readme_excerpt", return_value=None),
        mock.patch.object(cli, "classify_repository", side_effect=classifier) as mocked,
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        cli.run()
    return stdout, stderr, mocked


class CredentialResolutionTests(unittest.TestCase):
    def test_run_uses_the_credentials_module_and_logs_only_the_source_label(self):
        credentials = credentials_module()
        client = FakeOrganizationAPI()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(os.environ, hermetic({}), clear=True),
            mock.patch.object(sys, "argv", ["git-star-list-sort", "--limit", "1"]),
            mock.patch.object(cli, "GitHubAPI", return_value=client) as github_api,
            mock.patch.object(cli, "fetch_readme_excerpt", return_value=None),
            mock.patch.object(cli, "classify_repository", side_effect=classification()),
            mock.patch.object(cli.credentials, "resolve_github_token") as github_token,
            mock.patch.object(cli.credentials, "resolve_jev_credentials") as jev_key,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            github_token.return_value = (
                "github-test-token",
                credentials.STAR_LISTS_TOKEN_ENV,
            )
            jev_key.return_value = ("jev-test-key", credentials.JEV_API_KEY_ENV)
            cli.run()

        github_token.assert_called_once()
        jev_key.assert_called_once()
        # The resolved token must reach the GitHub client, and only the source
        # label may be logged.
        github_api.assert_called_once_with("github-test-token")
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, stderr.getvalue())
        self.assertNotIn("github-test-token", stderr.getvalue())
        self.assertNotIn("jev-test-key", stderr.getvalue())
        self.assertIn(credentials.JEV_API_KEY_ENV, stderr.getvalue())
        self.assertNotIn("github-test-token", stderr.getvalue())
        self.assertNotIn("jev-test-key", stderr.getvalue())

    def test_missing_github_token_exits_two_without_a_traceback(self):
        credentials = credentials_module()
        with (
            mock.patch.dict(
                os.environ,
                hermetic({credentials.JEV_API_KEY_ENV: "jev-test-key"}),
                clear=True,
            ),
            mock.patch.object(
                sys, "argv", ["git-star-list-sort", "--limit", "1"]
            ),
            mock.patch.object(
                cli.credentials.subprocess,
                "run",
                side_effect=FileNotFoundError("gh"),
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as caught,
        ):
            cli.main()
        self.assertEqual(2, caught.exception.code)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, stderr.getvalue())
        self.assertIn("gh auth login", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_missing_jev_key_exits_two_naming_the_variable(self):
        credentials = credentials_module()
        with (
            mock.patch.dict(
                os.environ,
                hermetic({credentials.STAR_LISTS_TOKEN_ENV: "github-test-token"}),
                clear=True,
            ),
            mock.patch.object(
                sys, "argv", ["git-star-list-sort", "--limit", "1"]
            ),
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            self.assertRaises(SystemExit) as caught,
        ):
            cli.main()
        self.assertEqual(2, caught.exception.code)
        self.assertIn(credentials.JEV_API_KEY_ENV, stderr.getvalue())
        self.assertNotIn("github-test-token", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())


class ReportOnlyDefaultTests(unittest.TestCase):
    def test_default_classifies_only_listed_repositories_and_reports_skips(self):
        client = FakeOrganizationAPI()
        stdout, stderr, classify = run_cli(["--limit", "0"], client)

        report = json.loads(stdout.getvalue())
        self.assertEqual(
            ["old/tool"],
            [item["repository"]["name_with_owner"] for item in report["results"]],
        )
        self.assertEqual(1, report["unlisted_skipped"])
        self.assertEqual(1, classify.call_count)
        self.assertIn("unlisted stars skipped", stderr.getvalue())
        self.assertIn("--include-unlisted", stderr.getvalue())

    def test_include_unlisted_opt_in_classifies_every_star(self):
        client = FakeOrganizationAPI()
        stdout, _, classify = run_cli(["--include-unlisted"], client)

        report = json.loads(stdout.getvalue())
        self.assertEqual(2, len(report["results"]))
        self.assertEqual(0, report["unlisted_skipped"])
        self.assertEqual(2, classify.call_count)

    def test_skip_notice_says_how_many_were_classified(self):
        # The notice must make a near-empty run obvious: "1 classified" out of a
        # 100-star batch explains why the report is tiny.
        client = FakeOrganizationAPI()
        _, stderr, _ = run_cli(["--limit", "0"], client)

        self.assertIn("1 classified", stderr.getvalue())

    def test_nothing_to_classify_is_called_out_explicitly(self):
        # A run where every fetched star is unlisted does full work for no result,
        # so it must not look like a silent success.
        client = FakeOrganizationAPI()
        client.star_edges = [
            {"starredAt": "2026-01-02T00:00:00Z", "node": repo("R_x", "x/tool")}
        ]
        stdout, stderr, classify = run_cli(["--limit", "0"], client)

        self.assertEqual([], json.loads(stdout.getvalue())["results"])
        self.assertEqual(0, classify.call_count)
        self.assertIn("0 classified", stderr.getvalue())
        self.assertIn("Nothing to classify", stderr.getvalue())
        self.assertIn("--include-unlisted", stderr.getvalue())

    def test_limit_bounds_the_number_classified(self):
        # --limit is the batch guard: only N stars are fetched and classified, so a
        # large star count cannot turn into a large batch. Recency ordering is
        # GitHub's (STARRED_AT DESC, asserted against the live API elsewhere); the
        # fake serves edges in the order it is given them.
        client = FakeOrganizationAPI()
        client.star_edges = [
            {
                "starredAt": f"2026-01-{day:02d}T00:00:00Z",
                "node": repo(f"R_{day}", f"owner/repo{day}"),
            }
            for day in range(1, 11)
        ]
        stdout, _, classify = run_cli(["--limit", "3", "--include-unlisted"], client)

        names = [
            item["repository"]["name_with_owner"]
            for item in json.loads(stdout.getvalue())["results"]
        ]
        self.assertEqual(3, len(names))
        self.assertEqual(3, classify.call_count)
        # The stars query must be asked for at most the requested page size.
        requests = [
            variables for query, variables in client.calls if query == STARS_QUERY
        ]
        self.assertTrue(requests)
        self.assertLessEqual(requests[0]["first"], 3)

    def test_classification_alone_issues_no_mutation(self):
        client = FakeOrganizationAPI()
        run_cli(["--limit", "0"], client)

        self.assertEqual([], client.mutations())
        self.assertTrue(
            all(query.lstrip().startswith("query ") for query in client.read_queries())
        )

    def test_parser_exposes_include_unlisted_as_a_flag(self):
        arguments = cli.parser().parse_args([])
        self.assertFalse(arguments.include_unlisted)
        enabled = cli.parser().parse_args(["--include-unlisted"])
        self.assertTrue(enabled.include_unlisted)
        self.assertIn("--include-unlisted", cli.parser().format_help())

    def test_skip_notice_is_absent_when_nothing_was_skipped(self):
        client = FakeOrganizationAPI()
        client.members["UL_tools"].update({"R_new", "R_listed"})
        stdout, stderr, classify = run_cli(["--limit", "0"], client)

        self.assertEqual(2, classify.call_count)
        self.assertEqual(0, json.loads(stdout.getvalue())["unlisted_skipped"])
        self.assertNotIn("unlisted stars skipped", stderr.getvalue())

    def test_all_stars_unlisted_still_reports_the_skip_loudly(self):
        client = FakeOrganizationAPI()
        client.members["UL_tools"] = set()
        stdout, stderr, classify = run_cli(["--limit", "0"], client)

        self.assertEqual([], json.loads(stdout.getvalue())["results"])
        self.assertEqual(2, json.loads(stdout.getvalue())["unlisted_skipped"])
        self.assertEqual(0, classify.call_count)
        self.assertIn("unlisted stars skipped", stderr.getvalue())


class DescribeListsTests(unittest.TestCase):
    def test_describe_lists_uses_committed_descriptions(self):
        client = FakeOrganizationAPI()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lists.json"
            path.write_text(
                json.dumps(
                    {
                        "generated_model": "jev-latest",
                        "lists": {"Developer Tools": "Committed tools"},
                    }
                ),
                encoding="utf-8",
            )
            _, _, classify = run_cli(
                ["--describe-lists", str(path), "--limit", "1", "--include-unlisted"],
                client,
            )

        criteria = classify.call_args.args[1]
        self.assertEqual("Developer Tools: Committed tools", criteria["UL_tools"])

    def test_missing_lists_json_falls_back_to_live_descriptions(self):
        client = FakeOrganizationAPI()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "absent.json"
            _, _, classify = run_cli(
                ["--describe-lists", str(path), "--limit", "1", "--include-unlisted"],
                client,
            )

        criteria = classify.call_args.args[1]
        self.assertEqual("Developer Tools: Tools", criteria["UL_tools"])


if __name__ == "__main__":
    unittest.main()
