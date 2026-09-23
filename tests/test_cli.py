from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

EMPTY_PATH = ""


class EntryPointTests(unittest.TestCase):
    def commands(self) -> list[list[str]]:
        executable = shutil.which("git-star-list-sort")
        self.assertIsNotNone(executable, "Install the package before running tests")
        return [[executable], [sys.executable, "-m", "git_star_list_sort"]]

    def hermetic_env(self, **overrides: str) -> dict[str, str]:
        """An environment with no ambient GitHub credentials or `gh` session.

        Without this, a developer with `gh` logged in would let the credential
        fallback succeed and the subprocess would make real network calls, so the
        test would neither be hermetic nor assert what it claims.
        """
        env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": tempfile.gettempdir(),
            "GH_CONFIG_DIR": tempfile.mkdtemp(),
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            "STAR_LISTS_TOKEN": "",
            "JEV_API_KEY": "",
            "TYPESAFE_API_KEY": "",
            "JEV_ENDPOINT": "",
        }
        env.update(overrides)
        return env

    def test_help_works_outside_the_project_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                with self.subTest(command=command):
                    result = subprocess.run(
                        [*command, "--help"],
                        cwd=directory,
                        env=self.hermetic_env(),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("--limit", result.stdout)
                    self.assertIn("--output", result.stdout)
                    self.assertIn("--include-unlisted", result.stdout)

    def test_missing_credentials_report_an_error_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                for missing in ("JEV_API_KEY", "STAR_LISTS_TOKEN"):
                    with self.subTest(command=command, missing=missing):
                        # `gh` is absent and TYPESAFE_API_KEY is unset, so a blanked
                        # variable leaves no source at all: it must be reported.
                        env = self.hermetic_env(
                            PATH=EMPTY_PATH,
                            JEV_API_KEY="test-key",
                            STAR_LISTS_TOKEN="github-test-token",
                        )
                        env[missing] = ""
                        result = subprocess.run(
                            command,
                            cwd=directory,
                            env=env,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=60,
                        )
                        self.assertEqual(2, result.returncode)
                        self.assertIn(missing, result.stderr)
                        self.assertNotIn("Traceback", result.stderr)

    def test_no_credentials_anywhere_is_reported_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                with self.subTest(command=command):
                    # Jev is checked first, so provide one to reach the GitHub check.
                    result = subprocess.run(
                        command,
                        cwd=directory,
                        env=self.hermetic_env(PATH=EMPTY_PATH, JEV_API_KEY="test-key"),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(2, result.returncode)
                    self.assertIn("STAR_LISTS_TOKEN", result.stderr)
                    self.assertIn("gh auth login", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    # A failure must never print a resolved secret.
                    self.assertNotIn("gho_", result.stderr)
                    self.assertNotIn("sk-", result.stderr)

    def test_no_jev_key_is_reported_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in self.commands():
                with self.subTest(command=command):
                    result = subprocess.run(
                        command,
                        cwd=directory,
                        env=self.hermetic_env(PATH=EMPTY_PATH),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=60,
                    )
                    self.assertEqual(2, result.returncode)
                    self.assertIn("JEV_API_KEY", result.stderr)
                    self.assertNotIn("Traceback", result.stderr)

    def test_describe_entry_point_is_installed_and_hermetic(self):
        executable = shutil.which("git-star-list-sort-describe")
        self.assertIsNotNone(executable, "Install the package before running tests")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [executable, "--help"],
                cwd=directory,
                env=self.hermetic_env(),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("--describe-lists-output", result.stdout)

    def test_missing_openrouter_key_is_reported_without_a_traceback(self):
        executable = shutil.which("git-star-list-sort-describe")
        self.assertIsNotNone(executable, "Install the package before running tests")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    executable,
                    "--describe-lists-output",
                    str(Path(directory) / "lists.json"),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                ],
                cwd=directory,
                env=self.hermetic_env(),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(2, result.returncode)
            self.assertIn("OPENROUTER_API_KEY", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
