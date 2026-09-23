from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from git_star_list_sort import credentials

GITHUB_SECRET = "github-test-token"
JEV_SECRET = "jev-test-key"


def gh_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """One captured subprocess result."""
    return mock.Mock(returncode=returncode, stdout=stdout, stderr=stderr)


def single_account_run(token: str) -> mock.Mock:
    """A `run` fake for a single-account session.

    `resolve_github_token` invokes `gh` twice with different commands, so the
    fake must answer them in order: `gh auth status` reports one account, then
    `gh auth token` prints the token. Returning one canned payload for every
    call would hand the status text back as a token.
    """
    return mock.Mock(
        side_effect=[
            gh_result(stdout="  Logged in to github.com account octocat (keyring)\n"),
            gh_result(stdout=f"{token}\n"),
        ]
    )


# `gh auth status` with no logged-in account captures empty stdout at exit 0.
no_accounts = gh_result()


# `gh auth status` prints one account line per logged-in account; `gh auth token`
# prints a bare token. Both are captured as stdout with exit code 0.
multiple_accounts_status = (
    "github.com\n"
    "  Logged in to github.com account octocat (keyring)\n"
    "  Logged in to github.com account hubot (keyring)\n"
)


class ResolveGitHubTokenTests(unittest.TestCase):
    def test_environment_variables_win_in_priority_order(self):
        for name in (
            credentials.STAR_LISTS_TOKEN_ENV,
            credentials.GH_TOKEN_ENV,
            credentials.GITHUB_TOKEN_ENV,
        ):
            with self.subTest(name=name):
                run = single_account_run(GITHUB_SECRET)
                env = {name: f"{name}: secret"}
                token, source = credentials.resolve_github_token(env, run=run)
                self.assertEqual(env[name], token)
                self.assertIn(name, source)
                run.assert_not_called()

    def test_falls_back_to_gh_auth_token_and_labels_the_source(self):
        run = single_account_run(GITHUB_SECRET)
        token, source = credentials.resolve_github_token({}, run=run)
        self.assertEqual(GITHUB_SECRET, token)
        self.assertIn("gh", source.casefold())
        status_command = " ".join(run.call_args_list[0].args[0])
        token_command = " ".join(run.call_args_list[-1].args[0])
        self.assertIn("auth status", status_command)
        self.assertIn("auth token", token_command)
        self.assertIn("github.com", token_command)

    def test_blank_environment_values_are_ignored(self):
        run = single_account_run(GITHUB_SECRET)
        env = {
            credentials.STAR_LISTS_TOKEN_ENV: "",
            credentials.GH_TOKEN_ENV: "  ",
            credentials.GITHUB_TOKEN_ENV: "",
        }
        token, _ = credentials.resolve_github_token(env, run=run)
        self.assertEqual(GITHUB_SECRET, token)
        self.assertEqual(2, run.call_count)

    def test_missing_gh_binary_is_reported_clearly(self):
        run = mock.Mock(side_effect=FileNotFoundError("gh"))
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        message = str(caught.exception)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, message)
        self.assertIn("gh auth login", message)

    def test_failed_gh_status_command_is_reported_without_a_traceback(self):
        run = mock.Mock(
            side_effect=[
                mock.Mock(returncode=1, stdout="", stderr="gh: not logged in"),
                mock.Mock(returncode=0, stdout="", stderr=""),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        message = str(caught.exception)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, message)
        self.assertIn("gh auth login", message)

    def test_failed_gh_token_command_is_reported_without_a_traceback(self):
        run = mock.Mock(
            side_effect=[
                no_accounts,
                mock.Mock(returncode=1, stdout="", stderr="gh: not logged in"),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        message = str(caught.exception)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, message)
        self.assertIn("gh auth login", message)

    def test_empty_gh_output_is_treated_as_missing_credentials(self):
        run = mock.Mock(
            side_effect=[
                no_accounts,
                mock.Mock(returncode=0, stdout="\n", stderr=""),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, str(caught.exception))

    def test_multiple_gh_accounts_require_an_explicit_token(self):
        run = mock.Mock(
            side_effect=[
                gh_result(stdout=multiple_accounts_status),
                gh_result(returncode=1, stderr="gh: not logged in"),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        message = str(caught.exception)
        self.assertIn(credentials.STAR_LISTS_TOKEN_ENV, message)
        self.assertIn("account", message.casefold())
        # The guard must fire before any token is requested.
        self.assertEqual(1, run.call_count)

    def test_single_gh_account_is_accepted(self):
        status = "  Logged in to github.com account octocat (keyring)\n"
        run = mock.Mock(
            side_effect=[
                mock.Mock(returncode=0, stdout=status, stderr=""),
                mock.Mock(returncode=0, stdout=f"{GITHUB_SECRET}\n", stderr=""),
            ]
        )
        token, source = credentials.resolve_github_token({}, run=run)
        self.assertEqual(GITHUB_SECRET, token)
        self.assertIn("gh", source.casefold())


class ResolveJevCredentialsTests(unittest.TestCase):
    def test_jev_api_key_takes_priority(self):
        env = {
            credentials.JEV_API_KEY_ENV: JEV_SECRET,
            credentials.TYPESAFE_API_KEY_ENV: "fallback-key",
        }
        api_key, source = credentials.resolve_jev_credentials(env)
        self.assertEqual(JEV_SECRET, api_key)
        self.assertIn(credentials.JEV_API_KEY_ENV, source)

    def test_typesafe_api_key_is_used_when_jev_key_is_absent(self):
        env = {credentials.TYPESAFE_API_KEY_ENV: "typesafe-key"}
        api_key, source = credentials.resolve_jev_credentials(env)
        self.assertEqual("typesafe-key", api_key)
        self.assertIn(credentials.TYPESAFE_API_KEY_ENV, source)

    def test_blank_values_are_ignored_and_missing_keys_are_reported(self):
        for env in ({}, {credentials.JEV_API_KEY_ENV: ""}):
            with self.subTest(env=env), self.assertRaises((RuntimeError, ValueError)):
                credentials.resolve_jev_credentials(env)


class SecretRedactionTests(unittest.TestCase):
    """A partially found token must never leak through an error path."""

    def test_github_token_never_appears_in_errors_or_stderr(self):
        stderr = io.StringIO()
        run = mock.Mock(
            side_effect=[
                no_accounts,
                gh_result(stdout="\n", stderr=GITHUB_SECRET, returncode=1),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token(
                {credentials.STAR_LISTS_TOKEN_ENV: ""}, run=run
            )
        message = str(caught.exception)
        self.assertNotIn(GITHUB_SECRET, message)
        self.assertNotIn(GITHUB_SECRET, stderr.getvalue())

    def test_jev_key_never_appears_in_errors_or_stderr(self):
        stderr = io.StringIO()
        with (
            contextlib.redirect_stderr(stderr),
            self.assertRaises((RuntimeError, ValueError)) as caught,
        ):
            credentials.resolve_jev_credentials(
                {credentials.TYPESAFE_API_KEY_ENV: JEV_SECRET}
                if False
                else {credentials.JEV_API_KEY_ENV: ""}
            )
        message = str(caught.exception)
        self.assertNotIn(JEV_SECRET, message)
        self.assertNotIn(JEV_SECRET, stderr.getvalue())

    def test_resolved_sources_never_contain_the_secret(self):
        token, source = credentials.resolve_github_token(
            {credentials.STAR_LISTS_TOKEN_ENV: GITHUB_SECRET}
        )
        self.assertEqual(GITHUB_SECRET, token)
        self.assertNotIn(GITHUB_SECRET, source)
        api_key, jev_source = credentials.resolve_jev_credentials(
            {credentials.JEV_API_KEY_ENV: JEV_SECRET}
        )
        self.assertEqual(JEV_SECRET, api_key)
        self.assertNotIn(JEV_SECRET, jev_source)

    def test_gh_json_output_is_not_echoed_raw(self):
        # Even when gh prints a token-shaped diagnostic, no exception text
        # may include it.
        run = mock.Mock(
            side_effect=[
                no_accounts,
                gh_result(stderr=json.dumps({"token": GITHUB_SECRET}), returncode=1),
            ]
        )
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            credentials.resolve_github_token({}, run=run)
        self.assertNotIn(GITHUB_SECRET, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
