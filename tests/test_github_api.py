from __future__ import annotations

import unittest
from unittest import mock

from github_star_organizer_jev.github_api import STARS_QUERY, starred_repositories
from tests.helpers import FakeGraphQL, repo


class StarPaginationTests(unittest.TestCase):
    def test_limit_fetches_only_the_requested_sample(self):
        client = FakeGraphQL()
        client.star_edges = [
            {"starredAt": "2026-01-01T00:00:00Z", "node": repo(f"R_{i}", f"owner/r{i}")}
            for i in range(201)
        ]
        progress = mock.Mock()

        stars = starred_repositories(client, limit=10, on_progress=progress)

        self.assertEqual(10, len(stars))
        self.assertEqual([(STARS_QUERY, {"cursor": None, "first": 10})], client.calls)
        progress.assert_called_once_with(10, 201)

    def test_limit_counts_accessible_repositories_across_pages(self):
        client = FakeGraphQL(page_size=2)
        client.star_edges = [
            {"starredAt": "2026-01-01T00:00:00Z", "node": node}
            for node in [
                None,
                repo("R_1", "o/one"),
                None,
                repo("R_2", "o/two"),
                repo("R_3", "o/three"),
            ]
        ]
        progress = mock.Mock()

        stars = starred_repositories(client, limit=2, on_progress=progress)

        self.assertEqual(["R_1", "R_2"], [star["id"] for star in stars])
        self.assertEqual(
            [2, 1, 1], [variables["first"] for _, variables in client.calls]
        )
        self.assertEqual(
            [mock.call(1, 5), mock.call(1, 5), mock.call(2, 5)], progress.call_args_list
        )

    def test_unlimited_fetch_keeps_pagination_and_reports_each_page(self):
        client = FakeGraphQL(page_size=1)
        progress = mock.Mock()

        stars = starred_repositories(client, on_progress=progress)

        self.assertEqual(["R_new", "R_listed"], [star["id"] for star in stars])
        self.assertEqual([mock.call(1, 2), mock.call(2, 2)], progress.call_args_list)
