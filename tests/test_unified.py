"""The unified CLI flow: one command that describes, classifies, and applies.

Behavior under test (spec approved with the user):

- An automatic describe-check runs before classification and never blocks it:
  no LLM call when nothing is missing, a loud stderr warning naming the Lists
  and OPENROUTER_API_KEY when the key is absent, and no crash when the check
  itself fails.
- ``--apply`` previews the net-new memberships with the same function apply
  executes, asks a y/N question on a TTY, and fails closed on a non-TTY
  without ``--yes``.
- ``--dry-run`` performs zero mutations; ``--yes`` alone is a no-op.
- A bare invocation prints a short guide instead of classifying.
- The viewer's Lists are fetched once for describe-check plus classify;
  apply's own re-fetch at apply time stays as a safety feature.
- The deprecated ``-apply``/``-describe`` shims keep their parsers working.

The feature is written test-first. Where a test pins behavior that does not
exist yet, its failure must name the missing piece rather than look like a bug
in the fixture, and every fixture stays off the network.
"""

from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_star_list_sort import apply, cli
from git_star_list_sort.github_api import (
    LISTS_QUERY,
)
from tests.helpers import FakeMutationGraphQL, FakeNotTTY, FakeTTY

CLI_NAME = "git-star-list-sort"
# The Lists fixture carries a committed GitHub description, so with an empty
# lists file the describe-check reports this List as missing deterministically.
MISSING_LIST = "Developer Tools"


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
    """Environment for a hermetic run (no real credentials anywhere).

    ``mock.patch.dict(..., clear=True)`` wipes the opt-out flag that stops the
    credential resolvers reading a developer's ``.env`` or ``~/.config`` file,
    so it must be re-added here.
    """
    merged = {
        "GIT_STAR_LIST_SORT_NO_DOTENV": "1",
        "OPENROUTER_MODEL": "",
        **environment,
    }
    # Keep the unified flow's describe-check away from the live OpenRouter API:
    # unless a test explicitly provides a key, none is set.
    merged.setdefault("OPENROUTER_API_KEY", "")
    return merged


def run_cli(
    argv,
    client,
    classify=None,
    env=None,
    stdin=None,
    input_responses=(),
    main=False,
    catch_exit=False,
    forbid_input=False,
    spy_apply=False,
    urlopen=None,
):
    """Run the CLI hermetically and return (stdout, stderr, calls).

    Only ``cli.GitHubAPI`` and the Jev classifier are replaced, so the unified
    flow's describe-check runs for real against the fake GitHub client and the
    environment's (empty) OPENROUTER_API_KEY. ``catch_exit`` captures
    SystemExit for the fail-closed and guide paths, ``forbid_input`` fails the
    test if the y/N prompt fires where no prompt may appear, ``spy_apply``
    wraps apply.apply_report so tests can assert that the unified flow reuses
    the apply implementation, and ``urlopen`` patches urllib.request.urlopen
    (the OpenRouter endpoint) for tests that must observe or forbid LLM calls.
    """
    environment = {
        "STAR_LISTS_TOKEN": "github-test-token",
        "JEV_API_KEY": "jev-test-key",
        "GH_TOKEN": "",
        "GITHUB_TOKEN": "",
        **(env or {}),
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    prompts: list[str] = []
    exit_error: SystemExit | None = None

    def fake_input(prompt=""):
        if forbid_input:
            raise AssertionError("input() must not be called in this run")
        prompts.append(prompt)
        return input_responses[len(prompts) - 1] if input_responses else ""

    # ExitStack instead of a parenthesized with-tuple: this helper needs a
    # handle on several patches at once, and Python 3.12 rejects combining an
    # item-level ``as`` with an outer ``as`` in that syntax.
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            mock.patch.dict(os.environ, hermetic(environment), clear=True)
        )
        stack.enter_context(mock.patch.object(sys, "argv", [CLI_NAME, *argv]))
        # Hermetic lists file: a path that does not exist, so no test reads the
        # developer's real checkout lists.json (which drifts and would make
        # assertions depend on live GitHub data).
        stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"STAR_LISTS_FILE": str(Path(tempfile.gettempdir()) / "no-such-lists.json")},
            )
        )
        stack.enter_context(mock.patch.object(cli, "GitHubAPI", return_value=client))
        stack.enter_context(
            mock.patch.object(cli, "fetch_readme_excerpt", return_value=None)
        )
        classify_mock = stack.enter_context(
            mock.patch.object(
                cli, "classify_repository", side_effect=classify or classification()
            )
        )
        stack.enter_context(mock.patch.object(sys, "stdin", new=stdin))
        stack.enter_context(
            mock.patch.object(builtins, "input", side_effect=fake_input)
        )
        # Recorded always: apply.apply_report is the one function both the
        # preview and the execution must go through.
        apply_spy = stack.enter_context(
            mock.patch.object(apply, "apply_report", wraps=apply.apply_report)
        )
        if urlopen is not None:
            stack.enter_context(
                mock.patch("urllib.request.urlopen", side_effect=urlopen)
            )
        stack.enter_context(contextlib.redirect_stdout(stdout))
        stack.enter_context(contextlib.redirect_stderr(stderr))
        try:
            if main:
                cli.main()
            else:
                cli.run()
        except SystemExit as error:  # only captured when catch_exit is set
            if not catch_exit:
                raise
            exit_error = error
    return (
        stdout,
        stderr,
        {
            "classify": classify_mock,
            "prompts": prompts,
            "exit": exit_error,
            "apply": apply_spy,
        },
    )


def capture_prompt_text(argv, client, input_answer):
    """Return the question text the CLI shows before applying."""
    _, stderr, calls = run_cli(
        argv, client, input_responses=[input_answer], stdin=FakeTTY()
    )
    return calls["prompts"], stderr.getvalue()


class UnifiedApplyFlowTests(unittest.TestCase):
    """--apply previews, confirms, then mutates through apply.apply_report."""

    def setUp(self):
        self.client = FakeMutationGraphQL()

    def output_argv(self, directory: str) -> list[str]:
        return [
            "--include-unlisted",
            "--apply",
            "--output",
            str(Path(directory) / "report.json"),
        ]

    def test_apply_yes_prints_preview_then_real_summary_and_mutates(self):
        """--apply --yes: preview, then the real apply, then its summary."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            stdout, stderr, calls = run_cli(
                [*self.output_argv(directory), "--yes"],
                self.client,
                stdin=FakeNotTTY(),
            )

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("octocat", report["viewer_login"])

        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIn("memberships", printed)
        self.assertIn("Developer Tools", printed)
        # The fixture has one net-new repository (R_new) and one
        # already-assigned one (R_listed): both must be visible in the preview.
        self.assertRegex(printed, r"(?i)already[\s_-]*assigned")
        # The real summary of the executed apply, not the preview keys.
        self.assertIn("applied", printed)
        self.assertIn("no_matching_category", printed)
        self.assertNotIn("would_apply", printed)
        # Exactly one repository was actually moved by the real apply.
        self.assertEqual(1, len(self.client.mutations()))
        self.assertEqual({"R_new", "R_listed"}, self.client.members["UL_tools"])
        self.assertGreaterEqual(calls["apply"].call_count, 1)

    def test_apply_saves_the_report_file_even_after_a_successful_apply(self):
        """Requirement 8: --output must survive an apply run as valid JSON."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            run_cli(
                [*self.output_argv(directory), "--yes"],
                self.client,
                stdin=FakeNotTTY(),
            )

            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual("octocat", report["viewer_login"])
        self.assertEqual(2, len(report["results"]))

    def test_apply_tty_prompt_defaults_to_no_and_aborts_cleanly(self):
        """A TTY y/N question; anything but yes aborts without mutating."""
        # Assumption: the prompt uses builtins.input (patched here); the
        # recorded question must offer the No default as y/N guidance.
        with tempfile.TemporaryDirectory() as directory:
            prompts, _stderr = capture_prompt_text(
                [*self.output_argv(directory)],
                self.client,
                input_answer="",
            )

        self.assertEqual(1, len(prompts), "one y/N question expected")
        lowered = prompts[0].lower()
        self.assertIn("y", lowered)
        self.assertIn("n", lowered)
        self.assertEqual([], self.client.mutations())
        self.assertEqual({"R_listed"}, self.client.members["UL_tools"])

    def test_apply_tty_explicit_yes_answer_confirms_and_applies(self):
        """An explicit "y" confirms: apply runs and the summary is printed."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            stdout, stderr, _calls = run_cli(
                [*self.output_argv(directory)],
                self.client,
                input_responses=["y"],
                stdin=FakeTTY(),
            )
            # Assert inside the block: the temporary directory is removed after it.
            self.assertTrue(output.exists())
            self.assertEqual(1, len(self.client.mutations()))
            self.assertEqual({"R_new", "R_listed"}, self.client.members["UL_tools"])
            self.assertIn("applied", stdout.getvalue() + stderr.getvalue())
            self.assertIn("no_matching_category", stdout.getvalue() + stderr.getvalue())

    def test_apply_on_non_tty_without_yes_fails_closed_with_zero_mutations(self):
        """A non-interactive apply without --yes must fail closed, not guess."""
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr, calls = run_cli(
                [*self.output_argv(directory)],
                self.client,
                stdin=FakeNotTTY(),
                catch_exit=True,
                forbid_input=True,
            )

        self.assertIsNotNone(calls["exit"], "expected a SystemExit")
        self.assertEqual(1, calls["exit"].code)
        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIn("--yes", printed)
        self.assertNotIn("Traceback", printed)
        self.assertEqual([], self.client.mutations())
        self.assertEqual({"R_listed"}, self.client.members["UL_tools"])

    def test_apply_dry_run_previews_and_performs_zero_mutations(self):
        """--apply --dry-run exits 0 and never reaches ASSIGN_LIST_MUTATION."""
        original = {
            list_id: set(members) for list_id, members in self.client.members.items()
        }
        with tempfile.TemporaryDirectory() as directory:
            stdout, stderr, calls = run_cli(
                [*self.output_argv(directory), "--dry-run"],
                self.client,
                stdin=FakeNotTTY(),
                catch_exit=True,
            )

        self.assertIsNone(calls["exit"], "--apply --dry-run must exit 0")
        self.assertEqual([], self.client.mutations())
        self.assertEqual(original, self.client.members)
        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIn("memberships", printed)
        self.assertIn("Developer Tools", printed)
        # A preview is allowed to use the dry-run keys; a real summary is not.
        self.assertNotIn('"applied"', printed)


class DescribeCheckTests(unittest.TestCase):
    """The automatic describe-check before classification.

    These drive the real flow: only ``cli.GitHubAPI`` and the Jev classifier
    are stubbed, the lists file selects which Lists count as missing, and the
    environment decides whether an OpenRouter key is resolvable.
    """

    def setUp(self):
        self.client = FakeMutationGraphQL()

    def run_with_lists_file(self, argv, document, env=None, **kwargs):
        with tempfile.TemporaryDirectory() as directory:
            lists_file = Path(directory) / "lists.json"
            lists_file.write_text(json.dumps(document), encoding="utf-8")
            output = Path(directory) / "report.json"
            stdout, stderr, calls = run_cli(
                [
                    "--describe-lists",
                    str(lists_file),
                    *argv,
                    "--output",
                    str(output),
                ],
                self.client,
                env=env,
                **kwargs,
            )
        return stdout, stderr, calls

    def test_missing_descriptions_without_a_key_warn_and_continue(self):
        """No key + missing descriptions: loud warning, then classify runs."""
        stdout, stderr, calls = self.run_with_lists_file(
            ["--include-unlisted"],
            {"lists": {}},
            stdin=FakeNotTTY(),
            catch_exit=True,
        )

        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIsNone(calls["exit"], "a missing key must not abort the sort")
        self.assertIn("descriptions", printed)
        self.assertIn("OPENROUTER_API_KEY", printed)
        self.assertIn(MISSING_LIST, printed)
        self.assertNotIn("Traceback", printed)
        self.assertEqual(2, calls["classify"].call_count)
        self.assertEqual([], self.client.mutations())

    def test_nothing_missing_needs_no_key_and_prints_no_warning(self):
        """Descriptions for every List: the check is cheap and quiet."""

        def no_http(*args, **kwargs):
            raise AssertionError("no OpenRouter request is allowed here")

        stdout, stderr, calls = self.run_with_lists_file(
            ["--include-unlisted"],
            {"lists": {MISSING_LIST: "Developer tools and utilities"}},
            urlopen=no_http,
        )

        printed = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn("OPENROUTER_API_KEY", printed)
        self.assertEqual(2, calls["classify"].call_count)

    def test_describe_check_failure_warns_and_classify_still_runs(self):
        """A network error inside the check must never abort the sort."""
        http_calls: list[object] = []

        def broken_http(*args, **kwargs):
            http_calls.append(args)
            raise OSError("network down")

        stdout, stderr, calls = self.run_with_lists_file(
            ["--include-unlisted"],
            {"lists": {}},
            env={"OPENROUTER_API_KEY": "openrouter-test-key"},
            urlopen=broken_http,
            catch_exit=True,
        )

        self.assertGreater(len(http_calls), 0)
        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIsNone(calls["exit"], "a failed check must not abort the sort")
        self.assertNotIn("Traceback", printed)
        self.assertEqual(2, calls["classify"].call_count)
        self.assertEqual([], self.client.mutations())

    def test_refresh_descriptions_without_a_key_warns_and_continues(self):
        """--refresh-descriptions needs a key; without one it must not abort."""
        stdout, stderr, calls = self.run_with_lists_file(
            ["--include-unlisted", "--refresh-descriptions"],
            {"lists": {}},
            stdin=FakeNotTTY(),
            catch_exit=True,
        )

        printed = stdout.getvalue() + stderr.getvalue()
        self.assertIsNone(calls["exit"], "a missing key must not abort the sort")
        self.assertIn("OPENROUTER_API_KEY", printed)
        self.assertIn(MISSING_LIST, printed)
        self.assertEqual(2, calls["classify"].call_count)
        self.assertEqual([], self.client.mutations())

    def test_classify_run_fetches_the_lists_query_exactly_once(self):
        """Describe-check and classify share one Lists fetch (section B)."""
        self.run_with_lists_file(["--include-unlisted"], {"lists": {}})

        self.assertEqual(1, len(self.client.executions(LISTS_QUERY)))


class BareInvocationTests(unittest.TestCase):
    """Running the command with no arguments prints a guide, not a report."""

    def setUp(self):
        self.client = FakeMutationGraphQL()

    def test_bare_invocation_prints_guide_and_exits_zero(self):
        stdout, stderr, calls = run_cli(
            [], self.client, stdin=FakeNotTTY(), catch_exit=True
        )

        self.assertIsNotNone(calls["exit"])
        self.assertEqual(0, calls["exit"].code)
        printed = stdout.getvalue() + stderr.getvalue()
        for flag in ("--include-unlisted", "--apply", "--dry-run"):
            self.assertIn(flag, printed)
        self.assertEqual(0, calls["classify"].call_count)
        self.assertEqual([], self.client.mutations())
        # No GitHub activity at all: the fake records every executed query.
        self.assertEqual([], self.client.calls)


class ParserFlagsTests(unittest.TestCase):
    """The new flags exist on the main parser with the right defaults."""

    def setUp(self):
        self.client = FakeMutationGraphQL()

    def test_apply_flag_defaults_to_false_and_is_a_boolean(self):
        arguments = cli.parser().parse_args([])
        self.assertFalse(arguments.apply)
        self.assertTrue(cli.parser().parse_args(["--apply"]).apply)

    def test_yes_flag_defaults_to_false_and_is_a_boolean(self):
        arguments = cli.parser().parse_args([])
        self.assertFalse(arguments.yes)
        self.assertTrue(cli.parser().parse_args(["--yes"]).yes)

    def test_dry_run_flag_defaults_to_false_and_is_a_boolean(self):
        arguments = cli.parser().parse_args([])
        self.assertFalse(arguments.dry_run)
        self.assertTrue(cli.parser().parse_args(["--dry-run"]).dry_run)

    def test_refresh_descriptions_flag_defaults_to_false(self):
        arguments = cli.parser().parse_args([])
        self.assertFalse(arguments.refresh_descriptions)
        self.assertTrue(
            cli.parser().parse_args(["--refresh-descriptions"]).refresh_descriptions
        )

    def test_yes_without_apply_is_a_no_op_that_never_mutates(self):
        """--yes alone must not error and must not touch GitHub."""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            _stdout, _stderr, calls = run_cli(
                ["--include-unlisted", "--yes", "--output", str(output)],
                self.client,
                stdin=FakeNotTTY(),
                catch_exit=True,
            )
            # Inside the block: TemporaryDirectory removes it on exit.
            self.assertIsNone(calls["exit"])
            self.assertEqual([], self.client.mutations())
            self.assertEqual({"R_listed"}, self.client.members["UL_tools"])
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(2, len(report["results"]))

    def test_single_dash_help_alias_exits_zero_with_usage(self):
        for argv in (["-help"], ["--help"], ["-h"]):
            with self.subTest(argv=argv):
                stdout, stderr, calls = run_cli(
                    argv, self.client, catch_exit=True, forbid_input=True
                )
                self.assertIsNotNone(calls["exit"])
                self.assertEqual(0, calls["exit"].code)
                printed = stdout.getvalue() + stderr.getvalue()
                self.assertIn("usage", printed)
                self.assertEqual([], self.client.calls)

    def test_help_text_mentions_the_new_flags(self):
        printed = cli.parser().format_help()
        for flag in ("--apply", "--dry-run", "--yes", "--refresh-descriptions"):
            self.assertIn(flag, printed)


class SafetyInvariantTests(unittest.TestCase):
    """Regression guards: only a confirmed --apply may mutate."""

    def setUp(self):
        self.client = FakeMutationGraphQL()

    def report_argv(self, directory: str) -> list[str]:
        return ["--output", str(Path(directory) / "report.json")]

    def snapshot(self) -> dict[str, set[str]]:
        return {
            list_id: set(members) for list_id, members in self.client.members.items()
        }

    def test_report_only_run_performs_zero_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            run_cli(
                ["--include-unlisted", *self.report_argv(directory)],
                self.client,
                stdin=FakeNotTTY(),
            )
        self.assertEqual([], self.client.mutations())
        self.assertEqual({"R_listed"}, self.client.members["UL_tools"])

    def test_apply_dry_run_performs_zero_mutations(self):
        original = self.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            run_cli(
                [
                    "--include-unlisted",
                    "--apply",
                    "--dry-run",
                    *self.report_argv(directory),
                ],
                self.client,
                stdin=FakeNotTTY(),
            )
        self.assertEqual([], self.client.mutations())
        self.assertEqual(original, self.snapshot())

    def test_apply_on_non_tty_without_yes_performs_zero_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            run_cli(
                ["--include-unlisted", "--apply", *self.report_argv(directory)],
                self.client,
                stdin=FakeNotTTY(),
                catch_exit=True,
                forbid_input=True,
            )
        self.assertEqual([], self.client.mutations())
        self.assertEqual({"R_listed"}, self.client.members["UL_tools"])

    def test_only_a_confirmed_apply_performs_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            run_cli(
                [
                    "--include-unlisted",
                    "--apply",
                    "--yes",
                    *self.report_argv(directory),
                ],
                self.client,
                stdin=FakeNotTTY(),
            )
        self.assertEqual(1, len(self.client.mutations()))


class DeprecatedShimsTests(unittest.TestCase):
    """git-star-list-sort-apply / -describe keep their own entry points."""

    def test_apply_shim_accepts_report_and_dry_run(self):
        """-apply --report X --dry-run still validates without mutating."""
        client = FakeMutationGraphQL()
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "report.json"
            report_path.write_text(
                json.dumps(
                    {
                        "viewer_login": "octocat",
                        "existing_lists": [
                            {"id": "UL_tools", "name": "Developer Tools"}
                        ],
                        "results": [
                            {"repository": {"id": "R_new"}, "list_id": "UL_tools"}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, hermetic({}), clear=True),
                mock.patch.object(
                    apply,
                    "resolve_github_token",
                    return_value=("github-test-token", "STAR_LISTS_TOKEN"),
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "git-star-list-sort-apply",
                        "--report",
                        str(report_path),
                        "--dry-run",
                    ],
                ),
                mock.patch.object(apply, "GitHubAPI", return_value=client),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                apply.main()

        self.assertEqual([], client.mutations())
        self.assertEqual(1, json.loads(stdout.getvalue())["would_apply"])

    def test_describe_shim_accepts_check_and_lists_output(self):
        """-describe --check --describe-lists-output Y still reports."""
        from git_star_list_sort import describe

        client = FakeMutationGraphQL()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "lists.json"
            output.write_text(
                json.dumps({"lists": {MISSING_LIST: "already described"}}),
                encoding="utf-8",
            )
            with (
                mock.patch.dict(os.environ, hermetic({}), clear=True),
                mock.patch.object(
                    describe,
                    "resolve_github_token",
                    return_value=("github-test-token", "STAR_LISTS_TOKEN"),
                ),
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "git-star-list-sort-describe",
                        "--check",
                        "--describe-lists-output",
                        str(output),
                    ],
                ),
                mock.patch.object(describe, "GitHubAPI", return_value=client),
                contextlib.redirect_stderr(stderr),
            ):
                describe.main()

        self.assertIn("nothing to do", stderr.getvalue())
        self.assertEqual([], client.mutations())


if __name__ == "__main__":
    unittest.main()
