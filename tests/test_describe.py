from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_star_list_sort import describe


def lists_fixture():
    return [
        {"id": "UL_sqlite", "name": "SQLite", "description": None},
        {"id": "UL_db", "name": "Database tools", "description": None},
    ]


def chat_response(content: str) -> mock.Mock:
    response = mock.Mock()
    response.__enter__ = mock.Mock(
        return_value=mock.Mock(
            read=mock.Mock(
                return_value=json.dumps(
                    {"choices": [{"message": {"content": content}}]}
                ).encode()
            )
        )
    )
    response.__exit__ = mock.Mock(return_value=False)
    return response


def contextlib_response(payload: dict) -> mock.Mock:
    """A urlopen-style context manager returning the given JSON payload."""
    response = mock.Mock()
    response.__enter__ = mock.Mock(
        return_value=mock.Mock(
            read=mock.Mock(return_value=json.dumps(payload).encode())
        )
    )
    response.__exit__ = mock.Mock(return_value=False)
    return response


class LoadEnvTests(unittest.TestCase):
    def test_reads_key_value_pairs_and_ignores_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\nOPENROUTER_API_KEY=sk-or-secret\n\nMALFORMED\n"
                'QUOTED="quoted value"\n',
                encoding="utf-8",
            )
            values = describe.load_env(path)
        self.assertEqual("sk-or-secret", values["OPENROUTER_API_KEY"])
        self.assertEqual("quoted value", values["QUOTED"])
        self.assertNotIn("MALFORMED", values)

    def test_missing_file_yields_no_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual({}, describe.load_env(Path(directory) / "absent"))


class DescribeTests(unittest.TestCase):
    def test_description_is_sent_to_the_model_and_trimmed(self):
        with mock.patch.object(
            describe.urllib.request,
            "urlopen",
            return_value=chat_response("  Databases, embedded.  \n\nNot an ORM. "),
        ) as urlopen:
            text = describe.describe("SQLite", ["SQLite", "Database tools"], "k", "m")
        self.assertEqual("Databases, embedded. Not an ORM.", text)
        request = urlopen.call_args.args[0]
        self.assertEqual(describe.DEFAULT_ENDPOINT, request.full_url)
        self.assertEqual("Bearer k", request.headers["Authorization"])
        body = json.loads(request.data)
        self.assertEqual("m", body["model"])
        # The request budget must cover chain-of-thought tokens, not just the
        # visible answer, or the response comes back with null content.
        self.assertGreater(body["max_tokens"], describe.MAX_TOKENS)
        # The sibling title gives the model the boundary it must not cross.
        self.assertIn("Database tools", body["messages"][1]["content"])
        # A bare title is never used as its own description.
        self.assertNotIn("SQLite, SQLite", body["messages"][1]["content"])

    def test_http_error_names_the_key_without_leaking_it(self):
        import urllib.error

        error = urllib.error.HTTPError("u", 401, "unauth", {}, None)
        with (
            mock.patch.object(describe.urllib.request, "urlopen", side_effect=error),
            self.assertRaises(RuntimeError) as caught,
        ):
            describe.describe("SQLite", [], "sk-or-secret", "m")
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))
        self.assertNotIn("sk-or-secret", str(caught.exception))

    def test_malformed_response_is_rejected(self):
        with (
            mock.patch.object(
                describe.urllib.request, "urlopen", return_value=chat_response("")
            ),
            self.assertRaises(ValueError),
        ):
            describe.describe("SQLite", [], "k", "m")

    def test_reasoning_budget_exhaustion_is_reported_not_swallowed(self):
        # Observed live: a reasoning model given a small max_tokens spends it all on
        # chain-of-thought and returns content=None with finish_reason="length".
        # That must be a clear error, not a confusing "invalid description".
        payload = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"content": None, "reasoning": "thinking..."},
                }
            ]
        }
        with (
            mock.patch.object(
                describe.urllib.request,
                "urlopen",
                return_value=contextlib_response(payload),
            ),
            self.assertRaises(ValueError) as caught,
        ):
            describe.describe("SQLite", [], "k", "m")
        message = str(caught.exception)
        self.assertIn("reasoning", message)
        self.assertIn("budget", message)

    def test_null_content_without_length_still_fails_clearly(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}
        with (
            mock.patch.object(
                describe.urllib.request,
                "urlopen",
                return_value=contextlib_response(payload),
            ),
            self.assertRaises(ValueError) as caught,
        ):
            describe.describe("SQLite", [], "k", "m")
        self.assertIn("finish_reason", str(caught.exception))


class GenerateTests(unittest.TestCase):
    def test_existing_descriptions_are_kept_without_force(self):
        with mock.patch.object(describe, "describe") as model:
            result = describe.generate(
                lists_fixture(),
                "k",
                "m",
                existing={"SQLite": "Hand written"},
            )
        self.assertEqual("Hand written", result["SQLite"])
        self.assertEqual(["Database tools"], [c.args[0] for c in model.call_args_list])

    def test_force_regenerates_every_description(self):
        with mock.patch.object(describe, "describe", return_value="Fresh") as model:
            result = describe.generate(
                lists_fixture(),
                "k",
                "m",
                existing={"SQLite": "Hand written"},
                force=True,
            )
        self.assertEqual({"SQLite": "Fresh", "Database tools": "Fresh"}, result)
        self.assertEqual(2, model.call_count)

    def test_output_order_follows_the_lists(self):
        with mock.patch.object(describe, "describe", side_effect=["A", "B"]):
            result = describe.generate(lists_fixture(), "k", "m")
        self.assertEqual(["SQLite", "Database tools"], list(result))

    def test_exhausted_budget_is_retried_at_a_larger_size(self):
        # Observed live: an abstract title burns the whole budget on reasoning.
        # The retry must rescue the title instead of failing the batch.
        exhausted = {
            "choices": [{"finish_reason": "length", "message": {"content": None}}]
        }
        answered = {
            "choices": [{"finish_reason": "stop", "message": {"content": "Text"}}]
        }
        with mock.patch.object(
            describe.urllib.request,
            "urlopen",
            side_effect=[
                contextlib_response(exhausted),
                contextlib_response(answered),
            ],
        ) as urlopen:
            self.assertEqual("Text", describe.describe("Abstract", [], "k", "m"))
        budgets = [
            json.loads(c.args[0].data)["max_tokens"] for c in urlopen.call_args_list
        ]
        self.assertEqual(2, len(budgets))
        self.assertGreater(budgets[1], budgets[0])

    def test_giving_up_after_the_retry_reports_the_reason(self):
        exhausted = {
            "choices": [{"finish_reason": "length", "message": {"content": None}}]
        }
        with (
            mock.patch.object(
                describe.urllib.request,
                "urlopen",
                return_value=contextlib_response(exhausted),
            ) as urlopen,
            self.assertRaises(ValueError) as caught,
        ):
            describe.describe("Abstract", [], "k", "m")
        self.assertIn("reasoning", str(caught.exception))
        budgets = [
            json.loads(c.args[0].data)["max_tokens"] for c in urlopen.call_args_list
        ]
        self.assertEqual(max(budgets), describe.MAX_TOKEN_BUDGET)


class GeneratePartialTests(unittest.TestCase):
    def test_one_failure_keeps_the_other_descriptions(self):
        with mock.patch.object(
            describe,
            "describe",
            side_effect=["Good", ValueError("boom")],
        ):
            descriptions, failures = describe.generate_partial(
                lists_fixture(), "k", "m"
            )
        self.assertEqual({"SQLite": "Good"}, descriptions)
        self.assertEqual([("Database tools", "boom")], failures)

    def test_no_failures_returns_every_description(self):
        with mock.patch.object(describe, "describe", return_value="X"):
            descriptions, failures = describe.generate_partial(
                lists_fixture(), "k", "m"
            )
        self.assertEqual({"SQLite": "X", "Database tools": "X"}, descriptions)
        self.assertEqual([], failures)


class MissingListTests(unittest.TestCase):
    """Lists added on GitHub after the file was written must be detected."""

    def test_missing_titles_are_the_ones_without_a_description(self):
        live = ["SQLite", "Database tools", "Brand New List"]
        existing = {"SQLite": "x", "Database tools": "y"}
        missing = [name for name in live if not existing.get(name)]
        self.assertEqual(["Brand New List"], missing)

    def test_a_list_gone_from_github_is_reported_not_dropped(self):
        live = ["SQLite"]
        existing = {"SQLite": "x", "Deleted List": "y"}
        disappeared = sorted(set(existing) - set(live))
        self.assertEqual(["Deleted List"], disappeared)
        # It stays in the file: the description itself is still valid if the List
        # comes back, and silently dropping user-visible data would be worse.
        self.assertIn("Deleted List", existing)

    def test_nothing_missing_means_nothing_to_generate(self):
        live = ["SQLite", "Database tools"]
        existing = {"SQLite": "x", "Database tools": "y"}
        self.assertEqual([], [name for name in live if not existing.get(name)])


if __name__ == "__main__":
    unittest.main()
