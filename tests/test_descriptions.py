from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from git_star_list_sort import descriptions


def lists_fixture():
    return [
        {"id": "UL_tools", "name": "Developer Tools", "description": "Live tools"},
        {"id": "UL_docs", "name": "Docs", "description": None},
    ]


class LoadDescriptionsTests(unittest.TestCase):
    def write(self, directory: str, text: str) -> Path:
        path = Path(directory) / "lists.json"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_name_to_description_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.write(
                directory,
                '{"generated_model": "jev-latest", '
                '"lists": {"Developer Tools": "Curated CLI tools"}}',
            )
            self.assertEqual(
                {"Developer Tools": "Curated CLI tools"},
                descriptions.load_descriptions(path),
            )

    def test_missing_file_is_tolerated(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                {}, descriptions.load_descriptions(Path(directory) / "absent.json")
            )

    def test_unparseable_file_is_tolerated(self):
        for text in ("not json", "", "[1, 2, 3]", '{"lists": null}'):
            with tempfile.TemporaryDirectory() as directory:
                path = self.write(directory, text)
                with self.subTest(text=text):
                    self.assertEqual({}, descriptions.load_descriptions(path))


class BuildCriteriaTests(unittest.TestCase):
    def test_committed_description_beats_the_live_list_description(self):
        criteria = descriptions.build_criteria(
            lists_fixture(), {"Developer Tools": "Committed description"}
        )
        self.assertEqual("Developer Tools: Committed description", criteria["UL_tools"])

    def test_live_description_is_used_when_nothing_is_committed(self):
        criteria = descriptions.build_criteria(lists_fixture(), {})
        self.assertEqual("Developer Tools: Live tools", criteria["UL_tools"])

    def test_bare_name_is_used_when_no_description_exists_anywhere(self):
        criteria = descriptions.build_criteria(lists_fixture(), {})
        self.assertEqual("Docs: ", criteria["UL_docs"])

    def test_only_matching_names_override_and_other_lists_are_untouched(self):
        criteria = descriptions.build_criteria(
            lists_fixture(),
            {"Developer Tools": "Committed", "Unknown": "Ignored"},
        )
        self.assertEqual("Developer Tools: Committed", criteria["UL_tools"])
        self.assertEqual("Docs: ", criteria["UL_docs"])
        self.assertNotIn("Unknown", criteria.values())

    def test_empty_description_falls_back_to_the_live_one(self):
        criteria = descriptions.build_criteria(lists_fixture(), {"Developer Tools": ""})
        self.assertEqual("Developer Tools: Live tools", criteria["UL_tools"])


if __name__ == "__main__":
    unittest.main()
