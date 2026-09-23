from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest


class EntryPointTests(unittest.TestCase):
    def commands(self) -> list[list[str]]:
        executable = shutil.which("github-star-organizer-jev")
        self.assertIsNotNone(executable, "Install the package before running tests")
        return [[executable], [sys.executable, "-m", "github_star_organizer_jev"]]

    def test_help_works_outside_the_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                with self.subTest(command=command):
                    result = subprocess.run(
                        [*command, "--help"],
                        cwd=directory,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("--limit", result.stdout)
                    self.assertIn("--output", result.stdout)

    def test_missing_credentials_report_an_error_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                for missing in ("JEV_API_KEY", "STAR_LISTS_TOKEN"):
                    with self.subTest(command=command, missing=missing):
                        result = subprocess.run(
                            command,
                            cwd=directory,
                            env={
                                **os.environ,
                                "JEV_API_KEY": "test-key",
                                "STAR_LISTS_TOKEN": "github-test-token",
                                missing: "",
                            },
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(2, result.returncode)
                        self.assertIn(f"set {missing}", result.stderr)
                        self.assertNotIn("Traceback", result.stderr)
