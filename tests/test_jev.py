from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from github_star_organizer_jev import cli as jev
from tests.helpers import FakeGraphQL, repo


def response_payload(choice: str = "UL_tools") -> dict:
    return {
        "model": "jev-test",
        "answers": {
            "list": {
                "type": "choice",
                "choice": choice,
                "confidence": 0.8,
                "probabilities": {"UL_tools": 0.9, "UL_other": 0.1},
            }
        },
        "usage": {"input_tokens": 123, "output_tokens": 12},
    }


class ReadmeTests(unittest.TestCase):
    def test_excerpt_skips_badges_images_comments_and_blank_lines(self):
        markdown = """
<!-- internal note
still a comment -->
[![Build](https://ci.example/badge.svg)](https://ci.example/build)
![Logo][logo]
<picture><source srcset="dark.svg"><img src="logo.svg"></picture>
<img src="another-logo.svg" alt="Logo">

# Example

Search   code &amp; documentation.
Read the [guide](https://example.org/guide_(intro)) or [docs][docs].

[logo]: https://example.org/logo.svg
[docs]: https://example.org/docs
"""
        self.assertEqual(
            "# Example\nSearch code & documentation.\nRead the guide or docs.",
            jev.readme_excerpt(markdown),
        )

    def test_excerpt_is_limited_to_2000_characters_after_cleanup(self):
        description = "A naïve tool description. " * 150
        markdown = "![Build](badge.svg)\n\n" + description
        self.assertEqual(description[:2000], jev.readme_excerpt(markdown))

    def test_fetch_reads_the_excerpt_without_caching(self):
        client = mock.Mock(spec=jev.GitHubAPI)
        client.readme.return_value = "# Tool\n\nA useful tool."
        first = jev.fetch_readme_excerpt(client, "owner/tool")
        second = jev.fetch_readme_excerpt(client, "owner/tool")
        self.assertEqual("# Tool\nA useful tool.", first)
        self.assertEqual(first, second)
        self.assertEqual(
            [mock.call("owner/tool"), mock.call("owner/tool")],
            client.readme.call_args_list,
        )

    def test_unavailable_or_empty_readme_falls_back_without_output(self):
        stderr = io.StringIO()
        for markdown in (None, "", "![Logo](logo.svg)"):
            client = mock.Mock(spec=jev.GitHubAPI)
            client.readme.return_value = markdown
            with self.subTest(markdown=markdown), contextlib.redirect_stderr(stderr):
                self.assertIsNone(jev.fetch_readme_excerpt(client, "owner/tool"))
        self.assertEqual("", stderr.getvalue())


class JevTests(unittest.TestCase):
    def test_request_and_response_use_real_list_ids(self):
        repository = {
            "id": "R_one",
            "name_with_owner": "owner/tool",
            "topics": ["cli"],
            "readme_excerpt": "A CLI tool for searching code.",
        }
        criteria = {"UL_tools": "Tools: CLI tools", "UL_other": "Other: Miscellaneous"}
        with mock.patch.object(
            jev.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(response_payload()).encode()),
        ) as urlopen:
            result = jev.classify_repository(
                repository, criteria, "test-key", "jev-test"
            )

        request = urlopen.call_args.args[0]
        self.assertEqual("https://api.typesafe.ai/v1/systemone", request.full_url)
        self.assertEqual("Bearer test-key", request.get_header("Authorization"))
        payload = json.loads(request.data)
        self.assertEqual(repository, payload["state"])
        self.assertEqual(criteria, payload["questions"]["list"]["criteria"])
        self.assertEqual("choice", payload["questions"]["list"]["type"])
        self.assertEqual("UL_tools", result["list_id"])
        self.assertEqual(0.8, result["confidence"])
        self.assertEqual(123, result["usage"]["input_tokens"])
        self.assertNotIn("test-key", json.dumps(result))

    def test_rejects_unknown_list_and_invalid_confidence(self):
        invalid = response_payload()
        invalid["answers"]["list"]["confidence"] = float("nan")
        for payload in (response_payload("UL_invented"), invalid, {"answers": {}}):
            with (
                self.subTest(payload=payload),
                mock.patch.object(
                    jev.urllib.request,
                    "urlopen",
                    return_value=io.BytesIO(json.dumps(payload).encode()),
                ),
                self.assertRaisesRegex(ValueError, "invalid List classification"),
            ):
                jev.classify_repository(
                    {},
                    {"UL_tools": "Tools", "UL_other": "Other"},
                    "test-key",
                    "jev-test",
                )

    def test_retries_rate_limit_and_overload(self):
        errors = [
            urllib.error.HTTPError(
                jev.DEFAULT_JEV_ENDPOINT, code, "Retry", None, io.BytesIO()
            )
            for code in (429, 529)
        ]
        with (
            mock.patch.object(
                jev.urllib.request,
                "urlopen",
                side_effect=errors
                + [io.BytesIO(json.dumps(response_payload()).encode())],
            ),
            mock.patch.object(jev.time, "sleep") as sleep,
        ):
            result = jev.classify_repository(
                {}, {"UL_tools": "Tools", "UL_other": "Other"}, "test-key", "jev-test"
            )
        self.assertEqual("UL_tools", result["list_id"])
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_auth_error_does_not_retry_or_print_response_body(self):
        error = urllib.error.HTTPError(
            jev.DEFAULT_JEV_ENDPOINT, 401, "Unauthorized", None, io.BytesIO(b"test-key")
        )
        with (
            mock.patch.object(
                jev.urllib.request, "urlopen", side_effect=error
            ) as urlopen,
            self.assertRaisesRegex(RuntimeError, "Jev HTTP 401") as caught,
        ):
            jev.classify_repository({}, {"UL_tools": "Tools"}, "test-key", "jev-test")
        self.assertEqual(1, urlopen.call_count)
        self.assertNotIn("test-key", str(caught.exception))

    def test_main_fetches_stars_limits_judgments_and_only_reads_github(self):
        client = FakeGraphQL()
        stderr = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.dict(
                os.environ,
                {
                    "JEV_API_KEY": "test-key",
                    "STAR_LISTS_TOKEN": "github-test-token",
                    "JEV_ENDPOINT": "https://environment.example.test/v1/systemone",
                },
                clear=True,
            ),
            mock.patch.object(jev, "GitHubAPI", return_value=client),
            mock.patch.object(
                jev, "fetch_readme_excerpt", return_value="A developer tool."
            ) as readme,
            mock.patch.object(
                jev,
                "classify_repository",
                return_value={
                    "list_id": "UL_tools",
                    "confidence": 0.8,
                    "elapsed_seconds": 0.123,
                },
            ) as classify,
            contextlib.redirect_stderr(stderr),
        ):
            output = Path(temporary) / "classifications.json"
            with mock.patch.object(
                sys,
                "argv",
                [
                    "github-star-organizer-jev",
                    "--limit",
                    "1",
                    "--output",
                    str(output),
                    "--endpoint",
                    "https://argument.example.test/v1/systemone",
                ],
            ):
                jev.run()
            report = json.loads(output.read_text())

        self.assertEqual(2, report["starred_total"])
        self.assertEqual(1, len(report["results"]))
        self.assertEqual(
            "new/tool", report["results"][0]["repository"]["name_with_owner"]
        )
        self.assertEqual("Developer Tools", report["results"][0]["list_name"])
        self.assertEqual(1, classify.call_count)
        self.assertEqual(
            "https://argument.example.test/v1/systemone",
            classify.call_args.kwargs["endpoint"],
        )
        readme.assert_called_once_with(client, "new/tool")
        self.assertEqual(
            "A developer tool.", classify.call_args.args[0]["readme_excerpt"]
        )
        self.assertIn(jev.NO_CATEGORY, classify.call_args.args[1])
        self.assertEqual(
            "A developer tool.", report["results"][0]["repository"]["readme_excerpt"]
        )
        self.assertEqual(
            ["Jev [1/1] new/tool -> Developer Tools (confidence 0.80, 0.12s)"],
            stderr.getvalue().splitlines(),
        )
        self.assertTrue(
            all(query.lstrip().startswith("query ") for query, _ in client.calls)
        )

    def test_default_processes_all_stars_and_reports_no_matching_category(self):
        client = FakeGraphQL(page_size=3)
        client.star_edges = [
            {
                "starredAt": "2026-01-01T00:00:00Z",
                "node": repo(f"R_{i}", f"owner/repo{i}"),
            }
            for i in range(12)
        ]
        payload = response_payload(jev.NO_CATEGORY)
        payload["answers"]["list"]["probabilities"] = {
            "UL_tools": 0.1,
            jev.NO_CATEGORY: 0.9,
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "JEV_API_KEY": "test-key",
                    "STAR_LISTS_TOKEN": "github-test-token",
                    "JEV_ENDPOINT": "https://provider.example.test/v1/systemone",
                },
                clear=True,
            ),
            mock.patch.object(sys, "argv", ["github-star-organizer-jev"]),
            mock.patch.object(jev, "GitHubAPI", return_value=client),
            mock.patch.object(jev, "fetch_readme_excerpt", return_value=None),
            mock.patch.object(
                jev.urllib.request,
                "urlopen",
                side_effect=lambda *args, **kwargs: io.BytesIO(
                    json.dumps(payload).encode()
                ),
            ) as urlopen,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(stderr),
        ):
            jev.run()

        results = json.loads(stdout.getvalue())["results"]
        self.assertEqual(12, len(results))
        self.assertEqual(12, urlopen.call_count)
        self.assertTrue(all(item["list_id"] is None for item in results))
        self.assertTrue(
            all(item["list_name"] == "No matching category" for item in results)
        )
        self.assertTrue(
            all(item["repository"]["readme_excerpt"] is None for item in results)
        )
        self.assertEqual(0.9, results[0]["probabilities"][jev.NO_CATEGORY])
        self.assertEqual(12, len(stderr.getvalue().splitlines()))
        self.assertIn(
            "Jev [12/12] owner/repo11 -> No matching category", stderr.getvalue()
        )
        self.assertEqual(
            "https://provider.example.test/v1/systemone",
            urlopen.call_args.args[0].full_url,
        )
        request = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(
            {"UL_tools", jev.NO_CATEGORY}, set(request["questions"]["list"]["criteria"])
        )

    def test_reserves_one_choice_for_no_matching_category(self):
        client = FakeGraphQL()
        client.lists = [{"id": f"UL_{i}", "name": str(i)} for i in range(255)]
        with (
            mock.patch.dict(
                os.environ,
                {
                    "JEV_API_KEY": "test-key",
                    "STAR_LISTS_TOKEN": "github-test-token",
                },
                clear=True,
            ),
            mock.patch.object(sys, "argv", ["github-star-organizer-jev"]),
            mock.patch.object(jev, "GitHubAPI", return_value=client),
            self.assertRaisesRegex(ValueError, "254 Lists"),
        ):
            jev.run()

    def test_empty_stars_make_no_jev_requests(self):
        client = FakeGraphQL()
        client.star_edges = []
        stdout = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "JEV_API_KEY": "test-key",
                    "STAR_LISTS_TOKEN": "github-test-token",
                },
                clear=True,
            ),
            mock.patch.object(sys, "argv", ["github-star-organizer-jev"]),
            mock.patch.object(jev, "GitHubAPI", return_value=client),
            mock.patch.object(jev, "classify_repository") as classify,
            contextlib.redirect_stdout(stdout),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            jev.run()
        self.assertEqual([], json.loads(stdout.getvalue())["results"])
        classify.assert_not_called()
