from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from git_star_list_sort import apply
from git_star_list_sort.github_api import ASSIGN_LIST_MUTATION, LIST_ITEMS_QUERY
from tests.helpers import FakeGraphQL


class FakeListAPI(FakeGraphQL):
    def __init__(self, page_size=None):
        super().__init__(page_size=page_size)
        self.lists.append({"id": "UL_other", "name": "Other"})
        self.items = {"UL_tools": {"R_listed"}, "UL_other": {"R_new"}}
        self.membership_partial_flags = []

    def execute(self, query, variables, allow_partial=False):
        if query == LIST_ITEMS_QUERY:
            self.calls.append((query, variables))
            self.membership_partial_flags.append(allow_partial)
            ids = sorted(self.items[variables["id"]])
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
        if query == ASSIGN_LIST_MUTATION:
            self.calls.append((query, variables))
            repository_id = variables["input"]["itemId"]
            for list_id, items in self.items.items():
                items.discard(repository_id)
                if list_id in variables["input"]["listIds"]:
                    items.add(repository_id)
            return {
                "updateUserListsForItem": {
                    "lists": [
                        {"id": list_id} for list_id in variables["input"]["listIds"]
                    ]
                }
            }
        return super().execute(query, variables, allow_partial)

    def mutations(self):
        return [
            variables
            for query, variables in self.calls
            if query == ASSIGN_LIST_MUTATION
        ]


def report():
    return {
        "viewer_login": "octocat",
        "existing_lists": [{"id": "UL_tools"}, {"id": "UL_other"}],
        "results": [
            {"repository": {"id": "R_new"}, "list_id": "UL_tools"},
            {"repository": {"id": "R_listed"}, "list_id": "UL_tools"},
            {"repository": {"id": "R_unmatched"}, "list_id": None},
        ],
    }


class ApplyTests(unittest.TestCase):
    def test_preserves_memberships_paginates_and_skips_already_assigned_and_unmatched(
        self,
    ):
        client = FakeListAPI(page_size=1)
        client.items["UL_other"].add("R_earlier")

        summary = apply.apply_report(client, report())

        self.assertEqual(
            {
                "applied": 1,
                "already_assigned": 1,
                "no_matching_category": 1,
                "per_list": {"Developer Tools": 1},
            },
            summary,
        )
        self.assertEqual(
            [{"input": {"itemId": "R_new", "listIds": ["UL_other", "UL_tools"]}}],
            client.mutations(),
        )
        self.assertEqual([False, False, False], client.membership_partial_flags)
        self.assertIn("R_new", client.items["UL_other"])

    def test_rerun_does_not_repeat_successful_assignments(self):
        client = FakeListAPI()
        apply.apply_report(client, report())
        client.calls.clear()

        summary = apply.apply_report(client, report())

        self.assertEqual(
            {
                "applied": 0,
                "already_assigned": 2,
                "no_matching_category": 1,
                "per_list": {},
            },
            summary,
        )
        self.assertEqual([], client.mutations())

    def test_incomplete_mutation_confirmation_is_reported_as_failure(self):
        client = FakeListAPI()
        execute = client.execute

        def incomplete(query, variables, allow_partial=False):
            if query == ASSIGN_LIST_MUTATION:
                return {"updateUserListsForItem": {"lists": [{"id": "UL_tools"}]}}
            return execute(query, variables, allow_partial)

        with (
            mock.patch.object(client, "execute", side_effect=incomplete),
            self.assertRaisesRegex(RuntimeError, "did not confirm"),
        ):
            apply.apply_report(client, report())

    def test_dry_run_does_not_update_memberships(self):
        client = FakeListAPI()
        original = copy.deepcopy(client.items)

        summary = apply.apply_report(client, report(), dry_run=True)

        self.assertEqual(
            {
                "would_apply": 1,
                "already_assigned": 1,
                "no_matching_category": 1,
                "per_list": {"Developer Tools": 1},
            },
            summary,
        )
        self.assertEqual(original, client.items)
        self.assertEqual([], client.mutations())

    def test_rejects_wrong_user_and_deleted_destination_before_any_mutation(self):
        wrong_user = report()
        wrong_user["viewer_login"] = "someone-else"
        deleted_destination = FakeListAPI()
        deleted_destination.lists = [{"id": "UL_other"}]
        for client, source, message in (
            (FakeListAPI(), wrong_user, "different user"),
            (deleted_destination, report(), "no longer exist"),
        ):
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(ValueError, message),
            ):
                apply.apply_report(client, source)
            self.assertEqual([], client.mutations())

    def test_rejects_invalid_reports_before_any_api_call(self):
        duplicate = report()
        duplicate["results"].append(duplicate["results"][0])
        unknown = report()
        unknown["results"][0]["list_id"] = "UL_unknown"
        missing_id = report()
        del missing_id["results"][0]["list_id"]
        bad_repository = report()
        bad_repository["results"][0]["repository"]["id"] = ""
        for source in (None, {}, duplicate, unknown, missing_id, bad_repository):
            client = FakeListAPI()
            with (
                self.subTest(source=source),
                self.assertRaises((TypeError, ValueError)),
            ):
                apply.apply_report(client, source)
            self.assertEqual([], client.calls)

    def test_incomplete_membership_data_stops_before_any_mutation(self):
        for response in (
            {"node": None},
            {"node": {"items": {"nodes": [None], "pageInfo": {"hasNextPage": False}}}},
        ):
            client = FakeListAPI()
            execute = client.execute

            def partial(
                query,
                variables,
                allow_partial=False,
                *,
                response=response,
                execute=execute,
            ):
                if query == LIST_ITEMS_QUERY:
                    return response
                return execute(query, variables, allow_partial)

            with (
                self.subTest(response=response),
                mock.patch.object(client, "execute", side_effect=partial),
                self.assertRaises((TypeError, ValueError)),
            ):
                apply.apply_report(client, report())
            self.assertEqual([], client.mutations())

    def test_apply_command_needs_only_the_github_token(self):
        client = FakeListAPI()
        stdout = io.StringIO()
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(
                os.environ, {"STAR_LISTS_TOKEN": "github-test-token"}, clear=True
            ),
            mock.patch.object(apply, "GitHubAPI", return_value=client) as constructor,
            contextlib.redirect_stdout(stdout),
        ):
            path = Path(directory) / "classifications.json"
            path.write_text(json.dumps(report()), encoding="utf-8")
            with mock.patch.object(
                sys,
                "argv",
                ["github-star-organizer-jev-apply", "--report", str(path), "--dry-run"],
            ):
                apply.run()
        constructor.assert_called_once_with("github-test-token")
        self.assertEqual(1, json.loads(stdout.getvalue())["would_apply"])
        self.assertEqual([], client.mutations())
