"""Token discovery and client behaviour. No network."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from candidateminer import github
from candidateminer.github import GitHubClient, discover_token

_TOKEN = "gho_" + "x" * 36


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TokenDiscoveryTests(unittest.TestCase):
    def test_environment_wins(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": _TOKEN}, clear=False):
            self.assertEqual(discover_token(), _TOKEN)

    def test_falls_back_to_older_gh_config_get(self):
        """gh 2.4 has no `auth token`; Debian shipped it for years."""
        calls = []

        def fake_run(command, **_):
            calls.append(list(command))
            if list(command[:3]) == ["gh", "auth", "token"]:
                # Old gh prints its usage text to STDOUT and exits non-zero.
                return _completed(1, 'unknown command "token" for "gh auth"\n\nUsage: ...')
            return _completed(0, _TOKEN + "\n")

        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            subprocess, "run", side_effect=fake_run
        ):
            self.assertEqual(discover_token(), _TOKEN)
        self.assertEqual(len(calls), 2, "should have tried both commands")

    def test_usage_text_is_never_mistaken_for_a_token(self):
        """The exact bug: return code is the only trustworthy signal."""
        usage = 'unknown command "token" for "gh auth"\n\nUsage:  gh auth <command>'
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            subprocess, "run", return_value=_completed(1, usage)
        ):
            self.assertIsNone(discover_token())

    def test_rejects_implausibly_short_output(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            subprocess, "run", return_value=_completed(0, "nope\n")
        ):
            self.assertIsNone(discover_token())

    def test_missing_gh_is_not_an_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            subprocess, "run", side_effect=OSError("no gh")
        ):
            self.assertIsNone(discover_token())


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())

    def test_cache_prevents_a_second_request(self):
        client = GitHubClient(token=_TOKEN, cache_dir=self.cache)
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": True}, None)
        ) as request:
            self.assertEqual(client.get("/x"), {"ok": True})
            self.assertEqual(client.get("/x"), {"ok": True})
            self.assertEqual(request.call_count, 1)
        self.assertEqual(client.cache_hits, 1)

    def test_cache_survives_a_new_client(self):
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": True}, None)
        ):
            GitHubClient(token=_TOKEN, cache_dir=self.cache).get("/x")
        fresh = GitHubClient(token=_TOKEN, cache_dir=self.cache)
        with mock.patch.object(GitHubClient, "_request") as request:
            self.assertEqual(fresh.get("/x"), {"ok": True})
            request.assert_not_called()

    def test_corrupt_cache_entry_is_ignored(self):
        client = GitHubClient(token=_TOKEN, cache_dir=self.cache)
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": True}, None)
        ):
            client.get("/x")
        for path in self.cache.glob("*.json"):
            path.write_text("{corrupt", encoding="utf-8")
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": 2}, None)
        ) as request:
            self.assertEqual(client.get("/x"), {"ok": 2})
            request.assert_called_once()

    def test_no_cache_dir_means_no_caching(self):
        client = GitHubClient(token=_TOKEN, cache_dir=None)
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": True}, None)
        ) as request:
            client.get("/x")
            client.get("/x")
            self.assertEqual(request.call_count, 2)


class CacheNamespaceTests(unittest.TestCase):
    """get() and paginate() store different shapes; they must never share a key.

    The regression: one sha256(url) namespace meant a URL fetched through both
    handed get() the {payload, next} envelope as if it were the API payload.
    """

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())

    def test_get_and_paginate_do_not_share_a_cache_entry(self):
        client = GitHubClient(token=_TOKEN, cache_dir=self.cache)
        with mock.patch.object(GitHubClient, "_request", return_value=([1, 2], None)):
            self.assertEqual(list(client.paginate("/x")), [1, 2])
        with mock.patch.object(
            GitHubClient, "_request", return_value=({"ok": True}, None)
        ) as request:
            self.assertEqual(client.get("/x"), {"ok": True})
            request.assert_called_once()  # paginate's entry must not satisfy get
        with mock.patch.object(GitHubClient, "_request") as request:
            self.assertEqual(list(client.paginate("/x")), [1, 2])
            request.assert_not_called()  # and paginate still reads its own


class CredentialBoundaryTests(unittest.TestCase):
    """The Bearer token is re-decided per hop and never sent off-origin.

    The regression: _request used urlopen's default redirect handling, which
    re-sends the original headers -- token included -- to whatever host a
    Location header names.
    """

    def _captured_request(self, url):
        captured = []

        class _Response:
            headers = {}

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class _Opener:
            def open(self, request, timeout=None):
                captured.append(request)
                return _Response()

        import urllib.request

        with mock.patch.object(urllib.request, "build_opener", lambda *h: _Opener()):
            GitHubClient(token=_TOKEN)._request(url)
        return captured[0]

    def test_api_origin_request_is_authorized(self):
        request = self._captured_request("https://api.github.com/repos/o/r")
        self.assertTrue(request.has_header("Authorization"))

    def test_off_origin_request_carries_no_token(self):
        request = self._captured_request("https://codeload.github.com/blob")
        self.assertFalse(request.has_header("Authorization"))


class AuthStateTests(unittest.TestCase):
    def test_authenticated_reflects_token_presence(self):
        self.assertTrue(GitHubClient(token=_TOKEN).authenticated)
        self.assertFalse(GitHubClient(token=None).authenticated)


if __name__ == "__main__":
    unittest.main()
