"""scan.py: the deterministic half of the candidate-scan workflow.

The end-to-end tests use the real miner CLI against a fixture repo but stub
the harvest away: the local environment may hold live GitHub credentials, and
a test that silently performs network calls when they are present is exactly
the kind of environment-dependent behaviour this tool exists to avoid.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

import scan


def _fingerprint(char: str) -> str:
    return "sha256:" + char * 64


def _marker(fingerprint: str) -> str:
    return f"`candidate-miner:fingerprint {fingerprint}`"


class FakeClient:
    def __init__(self, issues=(), truncated=()):
        self.issues = list(issues)
        self.truncated = list(truncated)
        self.paths: list[str] = []

    def paginate(self, path: str):
        self.paths.append(path)
        yield from self.issues


class MarkerTests(unittest.TestCase):
    def test_marker_round_trips(self):
        fp = _fingerprint("a")
        self.assertEqual(scan.MARKER_RE.findall(_marker(fp)), [fp])

    def test_marker_tolerates_extra_whitespace(self):
        fp = _fingerprint("b")
        text = f"candidate-miner:fingerprint   {fp}"
        self.assertEqual(scan.MARKER_RE.findall(text), [fp])

    def test_legacy_html_comment_form_still_matches(self):
        """Issues filed before the sanitizer was understood may carry this."""
        fp = _fingerprint("c")
        text = f"<!-- candidate-miner:fingerprint {fp} -->"
        self.assertEqual(scan.MARKER_RE.findall(text), [fp])

    def test_truncated_digest_does_not_match(self):
        text = f"candidate-miner:fingerprint sha256:{'a' * 63}"
        self.assertEqual(scan.MARKER_RE.findall(text), [])

    def test_overlong_digest_does_not_match(self):
        text = f"candidate-miner:fingerprint sha256:{'a' * 65}"
        self.assertEqual(scan.MARKER_RE.findall(text), [])


class FiledFingerprintTests(unittest.TestCase):
    def test_collects_from_open_and_closed_issues(self):
        a, b = _fingerprint("a"), _fingerprint("b")
        client = FakeClient(
            issues=[
                {"state": "open", "body": f"intro\n{_marker(a)}"},
                {"state": "closed", "body": _marker(b)},
            ]
        )
        self.assertEqual(scan.filed_fingerprints(client, "o/r"), {a, b})
        self.assertIn("repos/o/r/issues", client.paths[0])
        self.assertIn("state=all", client.paths[0])
        self.assertIn(f"labels={scan.ISSUE_LABEL}", client.paths[0])

    def test_skips_pull_requests_and_empty_bodies(self):
        a = _fingerprint("a")
        client = FakeClient(
            issues=[
                {"body": _marker(a), "pull_request": {"url": "x"}},
                {"body": None},
                {},
            ]
        )
        self.assertEqual(scan.filed_fingerprints(client, "o/r"), set())

    def test_truncated_listing_aborts(self):
        client = FakeClient(truncated=["/repos/o/r/issues"])
        with self.assertRaises(scan.ScanError):
            scan.filed_fingerprints(client, "o/r")


class SelectTests(unittest.TestCase):
    @staticmethod
    def _candidate(char: str, score: int) -> dict:
        return {"fingerprint": _fingerprint(char), "score": {"value": score}}

    def test_ranks_by_score_then_fingerprint(self):
        low, tie_b, tie_a = (
            self._candidate("c", 10),
            self._candidate("b", 50),
            self._candidate("a", 50),
        )
        selected, proposable = scan.select([low, tie_b, tie_a], set(), cap=10)
        self.assertEqual(selected, [tie_a, tie_b, low])
        self.assertEqual(proposable, 3)

    def test_filed_candidates_are_excluded_before_the_cap(self):
        filed = {_fingerprint("a")}
        candidates = [self._candidate("a", 90), self._candidate("b", 10)]
        selected, proposable = scan.select(candidates, filed, cap=1)
        self.assertEqual([c["fingerprint"] for c in selected], [_fingerprint("b")])
        self.assertEqual(proposable, 1)

    def test_cap_applies_after_ranking(self):
        candidates = [self._candidate(c, s) for c, s in (("a", 10), ("b", 90))]
        selected, _ = scan.select(candidates, set(), cap=1)
        self.assertEqual(selected[0]["fingerprint"], _fingerprint("b"))


def _fixture_repo() -> Path:
    root = Path(tempfile.mkdtemp())
    src = root / "spring-core/src/main/java/org/springframework/util"
    src.mkdir(parents=True)
    (src / "Alive.java").write_text("package org.springframework.util;", encoding="utf-8")
    docs = root / "framework-docs/modules/ROOT/pages"
    docs.mkdir(parents=True)
    (docs / "p.adoc").write_text(
        '<bean class="org.springframework.gone.Missing"/>\n', encoding="utf-8"
    )
    return root


def _offline_runner(argv):
    """Real miner CLI for `run`; harvest is stubbed to fail as if offline."""
    if argv[0] == "harvest":
        return 1, "", "error: no network in tests\n"
    return scan.run_miner_cli(argv)


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.repo = _fixture_repo()
        self.workspace = Path(tempfile.mkdtemp())
        self.out_dir = self.workspace / "out"
        for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            original = os.environ.get(name)
            os.environ[name] = str(self.workspace / name.lower())
            self.addCleanup(self._restore_env, name, original)

    @staticmethod
    def _restore_env(name: str, original: str | None):
        if original is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = original

    def _main(self, client) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return scan.main(
                [
                    "--repo-slug", "o/r",
                    "--max", "3",
                    "--repo", str(self.repo),
                    "--ledger-dir", str(self.workspace / "ledger"),
                    "--out-dir", str(self.out_dir),
                ],
                runner=_offline_runner,
                client=client,
            )

    def _proposable(self) -> dict:
        return json.loads((self.out_dir / "proposable.json").read_text(encoding="utf-8"))

    def test_finds_and_selects_the_fixture_candidate(self):
        code = self._main(FakeClient())
        self.assertEqual(code, scan.EXIT_OK)

        payload = self._proposable()
        self.assertEqual(payload["counts"]["selected"], 1)
        self.assertEqual(
            payload["candidates"][0]["identity"], "org.springframework.gone.Missing"
        )
        # The failed harvest is a recorded warning, not a hard failure.
        self.assertTrue(any("flaky_test" in w for w in payload["warnings"]))

        output = (self.workspace / "github_output").read_text(encoding="utf-8")
        self.assertIn("count=1", output)
        summary = (self.workspace / "github_step_summary").read_text(encoding="utf-8")
        self.assertIn("org.springframework.gone.Missing", summary)

    def test_an_existing_issue_suppresses_the_candidate(self):
        self._main(FakeClient())
        fingerprint = self._proposable()["candidates"][0]["fingerprint"]

        client = FakeClient(
            issues=[{"state": "closed", "body": _marker(fingerprint)}]
        )
        code = self._main(client)
        self.assertEqual(code, scan.EXIT_OK)

        payload = self._proposable()
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(payload["counts"]["already_filed"], 1)
        output = (self.workspace / "github_output").read_text(encoding="utf-8")
        self.assertIn("count=0", output)

    def test_dry_run_writes_no_ledger(self):
        self._main(FakeClient())
        self.assertFalse((self.workspace / "ledger").exists())

    def test_miner_defect_propagates_exit_2(self):
        def defective(argv):
            if argv[0] == "run":
                return 2, "", "DEFECT: extractor broken\n"
            return _offline_runner(argv)

        with contextlib.redirect_stdout(io.StringIO()):
            code = scan.main(
                ["--repo-slug", "o/r", "--out-dir", str(self.out_dir)],
                runner=defective,
                client=FakeClient(),
            )
        self.assertEqual(code, scan.EXIT_DEFECT)
        self.assertFalse((self.out_dir / "proposable.json").exists())


if __name__ == "__main__":
    unittest.main()
