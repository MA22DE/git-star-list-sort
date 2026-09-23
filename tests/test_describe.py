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
        self.assertLessEqual(body["max_tokens"], 100)
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


if __name__ == "__main__":
    unittest.main()
