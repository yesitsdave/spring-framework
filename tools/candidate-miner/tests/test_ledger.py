"""Ledger behaviour. The load-bearing rule is that a decline is permanent."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidateminer.contract import Candidate, CostClass, Locus, Score, State
from candidateminer.ledger import Ledger, LedgerError


def _candidate(identity: str = "org.x.Y", path: str = "docs/a.adoc", line: int = 1) -> Candidate:
    inputs = {"extractor_precision_prior": 40}
    return Candidate(
        category="docs.dead_api_reference",
        cost_class=CostClass.B_SOURCE,
        miner_name="docs_dead_ref",
        miner_version="1.0.0",
        locus=Locus(path=path, line=line),
        identity=identity,
        evidence={"reference": identity},
        score=Score(value=40, inputs=inputs),
        pr_grouping_key=path,
    )


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.adoc").write_text("x", encoding="utf-8")

    def test_first_run_marks_everything_new(self):
        result = Ledger().merge([_candidate()], commit="c1", root=self.root)
        self.assertEqual(len(result.emitted), 1)
        self.assertEqual(len(result.newly_seen), 1)

    def test_second_run_carries_over_rather_than_re_proposing(self):
        ledger = Ledger()
        ledger.merge([_candidate()], commit="c1", root=self.root)
        result = ledger.merge([_candidate()], commit="c2", root=self.root)
        self.assertEqual(len(result.emitted), 1)
        self.assertEqual(result.newly_seen, [], "a seen candidate must not be new again")

    def test_line_drift_does_not_create_a_duplicate(self):
        ledger = Ledger()
        ledger.merge([_candidate(line=10)], commit="c1", root=self.root)
        result = ledger.merge([_candidate(line=800)], commit="c2", root=self.root)
        self.assertEqual(len(ledger), 1)
        self.assertEqual(result.newly_seen, [])


class DeclineTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.adoc").write_text("x", encoding="utf-8")
        self.candidate = _candidate()
        self.ledger = Ledger()
        self.ledger.merge([self.candidate], commit="c1", root=self.root)

    def test_declining_suppresses_permanently(self):
        self.ledger.set_state(
            self.candidate.fingerprint, State.DECLINED, reason="intentional"
        )
        result = self.ledger.merge([self.candidate], commit="c2", root=self.root)
        self.assertEqual(result.emitted, [], "a declined candidate must never resurface")
        self.assertEqual(result.suppressed, [self.candidate.fingerprint])

    def test_decline_survives_a_save_load_round_trip(self):
        path = self.root / "ledger.jsonl"
        self.ledger.set_state(self.candidate.fingerprint, State.DECLINED, reason="no")
        self.ledger.save(path)
        result = Ledger.load(path).merge([self.candidate], commit="c3", root=self.root)
        self.assertEqual(result.emitted, [])

    def test_decline_requires_a_reason(self):
        with self.assertRaises(LedgerError):
            self.ledger.set_state(self.candidate.fingerprint, State.DECLINED)

    def test_decline_is_terminal(self):
        self.ledger.set_state(self.candidate.fingerprint, State.DECLINED, reason="no")
        with self.assertRaises(LedgerError):
            self.ledger.set_state(self.candidate.fingerprint, State.QUEUED)

    def test_unknown_fingerprint_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.ledger.set_state("sha256:" + "0" * 64, State.QUEUED)


class OrphanTests(unittest.TestCase):
    """A decided candidate that vanishes is ambiguous, and the tool must not guess."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.adoc").write_text("x", encoding="utf-8")
        self.candidate = _candidate()
        self.ledger = Ledger()
        self.ledger.merge([self.candidate], commit="c1", root=self.root)
        self.ledger.set_state(self.candidate.fingerprint, State.DECLINED, reason="no")

    def test_missing_file_is_an_orphan_needing_a_human(self):
        (self.root / "docs" / "a.adoc").unlink()
        result = self.ledger.merge([], commit="c2", root=self.root)
        self.assertEqual(len(result.orphans), 1)
        self.assertEqual(result.vanished, [])

    def test_surviving_file_means_the_reference_was_fixed(self):
        result = self.ledger.merge([], commit="c2", root=self.root)
        self.assertEqual(result.vanished, [result.vanished[0]])
        self.assertEqual(result.orphans, [])

    def test_new_entries_are_never_reported_as_orphans(self):
        fresh = Ledger()
        fresh.merge([_candidate(identity="org.x.Z")], commit="c1", root=self.root)
        result = fresh.merge([], commit="c2", root=self.root)
        self.assertEqual(result.orphans, [])
        self.assertEqual(result.vanished, [])


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "docs").mkdir()
        (self.root / "docs" / "a.adoc").write_text("x", encoding="utf-8")

    def test_save_is_a_noop_when_nothing_changed(self):
        """Stops a scheduled job committing an identical file on every run."""
        path = self.root / "ledger.jsonl"
        ledger = Ledger()
        ledger.merge([_candidate()], commit="c1", root=self.root)
        self.assertTrue(ledger.save(path))
        self.assertFalse(ledger.save(path))

    def test_output_is_sorted_for_stable_diffs(self):
        path = self.root / "ledger.jsonl"
        ledger = Ledger()
        ledger.merge(
            [_candidate(identity=f"org.x.T{i}") for i in range(8)],
            commit="c1",
            root=self.root,
        )
        ledger.save(path)
        fingerprints = [
            line.split('"fingerprint":"')[1].split('"')[0]
            for line in path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(fingerprints, sorted(fingerprints))

    def test_corrupt_ledger_fails_loudly(self):
        path = self.root / "ledger.jsonl"
        path.write_text("{not json}\n", encoding="utf-8")
        with self.assertRaises(LedgerError):
            Ledger.load(path)


if __name__ == "__main__":
    unittest.main()
