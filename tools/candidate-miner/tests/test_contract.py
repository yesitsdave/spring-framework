"""Contract invariants -- above all, that fingerprints survive line drift."""

from __future__ import annotations

import unittest

from candidateminer.contract import (
    Candidate,
    ContractError,
    CostClass,
    Locus,
    Score,
    validate_record,
)


def _candidate(*, line: int = 10, path: str = "docs/a.adoc", identity: str = "org.x.Y") -> Candidate:
    inputs = {"extractor_precision_prior": 40, "copy_paste_risk": 10}
    return Candidate(
        category="docs.dead_api_reference",
        cost_class=CostClass.B_SOURCE,
        miner_name="docs_dead_ref",
        miner_version="1.0.0",
        locus=Locus(path=path, line=line),
        identity=identity,
        evidence={"reference": identity},
        score=Score(value=sum(inputs.values()), inputs=inputs),
        pr_grouping_key=path,
    )


class FingerprintTests(unittest.TestCase):
    def test_survives_line_drift(self):
        """The whole point: content moving down a file is not a new candidate."""
        self.assertEqual(
            _candidate(line=10).fingerprint, _candidate(line=9999).fingerprint
        )

    def test_survives_evidence_and_score_changes(self):
        original = _candidate()
        shifted = Candidate(
            category=original.category,
            cost_class=original.cost_class,
            miner_name=original.miner_name,
            miner_version="9.9.9",
            locus=Locus(path=original.locus.path, line=1),
            identity=original.identity,
            evidence={"reference": original.identity, "extra": "noise"},
            score=Score(value=1, inputs={"x": 1}),
            pr_grouping_key=original.pr_grouping_key,
            repo_commit="different",
        )
        self.assertEqual(original.fingerprint, shifted.fingerprint)

    def test_distinguishes_identity(self):
        self.assertNotEqual(
            _candidate(identity="org.x.Y").fingerprint,
            _candidate(identity="org.x.Z").fingerprint,
        )

    def test_distinguishes_path(self):
        self.assertNotEqual(
            _candidate(path="docs/a.adoc").fingerprint,
            _candidate(path="docs/b.adoc").fingerprint,
        )

    def test_is_not_order_or_field_dependent(self):
        """Separator use must stop a path/identity boundary shift colliding."""
        a = _candidate(path="docs/a", identity="b.C")
        b = _candidate(path="docs", identity="a.b.C")
        self.assertNotEqual(a.fingerprint, b.fingerprint)


class ValidationTests(unittest.TestCase):
    def test_accepts_a_well_formed_record(self):
        validate_record(_candidate().to_record())

    def test_rejects_missing_field(self):
        record = _candidate().to_record()
        del record["score"]
        with self.assertRaises(ContractError):
            validate_record(record)

    def test_rejects_score_not_matching_its_inputs(self):
        """Scores must be auditable: the total is the sum of its exposed parts."""
        record = _candidate().to_record()
        record["score"]["value"] = 999
        with self.assertRaises(ContractError):
            validate_record(record)

    def test_rejects_float_score(self):
        record = _candidate().to_record()
        record["score"]["value"] = 50.0
        record["score"]["inputs"] = {"a": 50.0}
        with self.assertRaises(ContractError):
            validate_record(record)

    def test_rejects_bad_fingerprint(self):
        record = _candidate().to_record()
        record["fingerprint"] = "nope"
        with self.assertRaises(ContractError):
            validate_record(record)

    def test_rejects_unknown_cost_class(self):
        record = _candidate().to_record()
        record["cost_class"] = "z_wat"
        with self.assertRaises(ContractError):
            validate_record(record)


class SerialisationTests(unittest.TestCase):
    def test_jsonl_is_single_line_and_stable(self):
        line = _candidate().to_jsonl()
        self.assertNotIn("\n", line)
        self.assertEqual(line, _candidate().to_jsonl())

    def test_no_wallclock_in_record(self):
        """Timestamps belong in the run manifest, never in a candidate record."""
        record = _candidate().to_record()
        self.assertNotIn("ran_at", record)
        self.assertNotIn("timestamp", record)


if __name__ == "__main__":
    unittest.main()
