"""reconcile.py: issue gestures fold into ledger state, declines held terminal."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import reconcile
from candidateminer.contract import canonical_json
from candidateminer.ledger import Ledger
from candidateminer.contract import State


def _fingerprint(char: str) -> str:
    return "sha256:" + char * 64


def _marker(fingerprint: str) -> str:
    return f"`candidate-miner:fingerprint {fingerprint}`"


def _issue(number: int, fingerprint: str, *, state="open", state_reason=None) -> dict:
    return {
        "number": number,
        "state": state,
        "state_reason": state_reason,
        "body": f"details\n\n{_marker(fingerprint)}\n",
    }


class FakeClient:
    def __init__(self, issues=(), closed_by="alice"):
        self.issues = list(issues)
        self.closed_by = closed_by
        self.truncated = []

    def paginate(self, path: str):
        yield from self.issues

    def get(self, path: str):
        return {"closed_by": {"login": self.closed_by}}


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.ledger_dir = Path(tempfile.mkdtemp())

    def _write_entry(self, fingerprint: str, state: str, *, stem: str = "docs_dead_ref"):
        entry = {
            "fingerprint": fingerprint, "state": state,
            "category": "docs.dead_api_reference", "path": "p.adoc",
            "identity": "x", "first_seen_commit": "abc", "last_seen_commit": "abc",
        }
        if state == "declined":
            entry["reason"] = "prior veto"
        with open(self.ledger_dir / f"{stem}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(canonical_json(entry) + "\n")

    def _state_of(self, fingerprint: str, stem: str = "docs_dead_ref") -> State:
        return Ledger.load(self.ledger_dir / f"{stem}.jsonl").get(fingerprint).state

    def _run(self, issues, closed_by="alice") -> reconcile.Report:
        return reconcile.reconcile(
            FakeClient(issues, closed_by=closed_by), "o/r", self.ledger_dir
        )

    def test_open_issue_queues_a_new_candidate(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "new")
        report = self._run([_issue(1, fp)])
        self.assertEqual(self._state_of(fp), State.QUEUED)
        self.assertEqual(report.changed_ledgers, ["docs_dead_ref"])

    def test_completed_close_marks_done(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "queued")
        self._run([_issue(1, fp, state="closed", state_reason="completed")])
        self.assertEqual(self._state_of(fp), State.DONE)

    def test_not_planned_close_declines_with_provenance(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "queued")
        self._run([_issue(4, fp, state="closed", state_reason="not_planned")], closed_by="bob")
        ledger = Ledger.load(self.ledger_dir / "docs_dead_ref.jsonl")
        entry = ledger.get(fp)
        self.assertIs(entry.state, State.DECLINED)
        self.assertEqual(entry.reason, "issue #4 closed as not planned")
        self.assertEqual(entry.decided_by, "bob")

    def test_reopened_issue_requeues_done_work(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "done")
        self._run([_issue(1, fp, state="open")])
        self.assertEqual(self._state_of(fp), State.QUEUED)

    def test_declined_is_held_terminal(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "declined")
        report = self._run([_issue(1, fp, state="open")])
        self.assertEqual(self._state_of(fp), State.DECLINED)
        self.assertEqual(len(report.conflicts), 1)
        self.assertEqual(report.changed_ledgers, [])

    def test_already_in_target_state_is_a_no_op(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "queued")
        report = self._run([_issue(1, fp)])
        self.assertEqual(report.transitions, [])
        self.assertEqual(report.changed_ledgers, [])

    def test_unknown_fingerprint_is_reported_not_fatal(self):
        report = self._run([_issue(1, _fingerprint("f"))])
        self.assertEqual(len(report.unknown), 1)

    def test_ambiguous_issue_is_skipped(self):
        fp_a, fp_b = _fingerprint("a"), _fingerprint("b")
        self._write_entry(fp_a, "new")
        issue = _issue(1, fp_a)
        issue["body"] += _marker(fp_b)
        report = self._run([issue])
        self.assertEqual(report.ambiguous, [1])
        self.assertEqual(self._state_of(fp_a), State.NEW)

    def test_pull_requests_are_ignored(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "new")
        pr = _issue(9, fp, state="closed", state_reason="completed")
        pr["pull_request"] = {"url": "x"}
        report = self._run([pr])
        self.assertEqual(self._state_of(fp), State.NEW)
        self.assertEqual(report.transitions, [])

    def test_truncated_listing_aborts_without_writing(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "new")
        client = FakeClient([_issue(1, fp)])
        client.truncated = ["/issues"]
        from candidateminer.github import GitHubError
        with self.assertRaises(GitHubError):
            reconcile.reconcile(client, "o/r", self.ledger_dir)
        self.assertEqual(self._state_of(fp), State.NEW)

    def test_cli_reports_changed_output(self):
        fp = _fingerprint("a")
        self._write_entry(fp, "new")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = reconcile.main(
                ["--repo-slug", "o/r", "--ledger-dir", str(self.ledger_dir)],
                client=FakeClient([_issue(1, fp)]),
            )
        self.assertEqual(code, reconcile.EXIT_OK)
        self.assertIn("new → queued", out.getvalue())


if __name__ == "__main__":
    unittest.main()
