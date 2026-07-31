"""flaky_test miner: recurrence gating, tree resolution, determinism, defects."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from candidateminer.contract import validate_record
from candidateminer.miners import MinerContext, MinerDefect
from candidateminer.miners.flaky_test import MINER


def _tree() -> Path:
    root = Path(tempfile.mkdtemp())
    src = root / "spring-core/src/test/java/org/springframework/core"
    src.mkdir(parents=True)
    (src / "RetryTemplateTests.java").write_text("class RetryTemplateTests {}\n")
    (src / "SoloTests.java").write_text("class SoloTests {}\n")
    return root


def _failed_run(run_id, *, tests, failed=1, task=":spring-core:java21Test", cls="test"):
    return {
        "schema_version": 1,
        "corpus": "ci_failures",
        "run_id": run_id,
        "workflow": "ci.yml",
        "run_started_at": f"2026-06-{run_id:02d}T10:00:00Z",
        "head_sha": "cafe",
        "html_url": f"https://example.test/runs/{run_id}",
        "failed_jobs": ["Linux | Java 21"],
        "log_available": True,
        "failed_tasks": [task],
        "test_summaries": [{"completed": 5000, "failed": failed, "skipped": 0}],
        "failing_tests": tests,
        "scan_url": f"https://ge.spring.io/s/run{run_id}",
        "classification": cls,
    }


def _retry_failure():
    return {
        "test_class": "RetryTemplateTests",
        "nested_classes": ["TimeoutTests"],
        "method": "retryableWithTimeoutExceededAfterSecondRetry",
        "display": "RetryTemplateTests > TimeoutTests > retryable...() FAILED",
        "cause": "java.lang.AssertionError",
        "at_file": "RetryTemplateTests.java",
        "at_line": 802,
    }


class FlakyTestMinerTests(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.out = Path(tempfile.mkdtemp())

    def _ctx(self, runs) -> MinerContext:
        (self.out / "ci_failures.corpus.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in runs), encoding="utf-8"
        )
        return MinerContext(
            root=self.root, commit="c", branch="main", options={"out_dir": str(self.out)}
        )

    def test_repeated_failure_becomes_a_candidate(self):
        runs = [
            _failed_run(8, tests=[_retry_failure()]),
            _failed_run(15, tests=[_retry_failure()], task=":spring-core:test"),
        ]
        (candidate,) = MINER.run(self._ctx(runs))
        validate_record(candidate.to_record())
        self.assertEqual(
            candidate.identity,
            "RetryTemplateTests.TimeoutTests.retryableWithTimeoutExceededAfterSecondRetry",
        )
        self.assertTrue(candidate.locus.path.endswith("RetryTemplateTests.java"))
        self.assertEqual(candidate.locus.line, 802)
        self.assertEqual(candidate.evidence["occurrence_count"], 2)
        # Cross-variant failures score higher than single-variant.
        self.assertEqual(candidate.score.inputs["task_variant_spread"], 20)

    def test_single_occurrence_stays_in_the_corpus(self):
        solo = dict(_retry_failure(), test_class="SoloTests", nested_classes=[])
        self.assertEqual(list(MINER.run(self._ctx([_failed_run(8, tests=[solo])]))), [])

    def test_mass_failure_is_breakage_not_flakiness(self):
        runs = [
            _failed_run(8, tests=[_retry_failure()], failed=250),
            _failed_run(15, tests=[_retry_failure()], failed=250),
        ]
        self.assertEqual(list(MINER.run(self._ctx(runs))), [])

    def test_unresolvable_class_cannot_become_a_candidate(self):
        """A test deleted since the failure cannot be traced to a line, so it does not exist."""
        ghost = dict(_retry_failure(), test_class="DeletedTests", nested_classes=[])
        runs = [_failed_run(8, tests=[ghost]), _failed_run(15, tests=[ghost])]
        self.assertEqual(list(MINER.run(self._ctx(runs))), [])

    def test_non_test_classifications_are_ignored(self):
        runs = [
            _failed_run(8, tests=[_retry_failure()], cls="javadoc"),
            _failed_run(15, tests=[_retry_failure()], cls="javadoc"),
        ]
        self.assertEqual(list(MINER.run(self._ctx(runs))), [])

    def test_fingerprint_survives_line_drift(self):
        runs_a = [
            _failed_run(8, tests=[_retry_failure()]),
            _failed_run(15, tests=[_retry_failure()]),
        ]
        moved = dict(_retry_failure(), at_line=900)
        runs_b = [_failed_run(8, tests=[moved]), _failed_run(15, tests=[moved])]
        (a,) = MINER.run(self._ctx(runs_a))
        (b,) = MINER.run(self._ctx(runs_b))
        self.assertEqual(a.fingerprint, b.fingerprint)

    def test_deterministic_output(self):
        runs = [
            _failed_run(8, tests=[_retry_failure()]),
            _failed_run(15, tests=[_retry_failure()]),
        ]
        ctx = self._ctx(runs)
        first = [c.to_record() for c in MINER.run(ctx)]
        second = [c.to_record() for c in MINER.run(ctx)]
        self.assertEqual(first, second)

    def test_implausibly_many_flakes_is_a_defect(self):
        src = self.root / "spring-core/src/test/java/org/springframework/core"
        runs = []
        for i in range(13):
            name = f"Gen{i}Tests"
            (src / f"{name}.java").write_text(f"class {name} {{}}\n")
            failure = dict(
                _retry_failure(), test_class=name, nested_classes=[], at_file=f"{name}.java"
            )
            runs.append(_failed_run(i + 1, tests=[failure]))
            runs.append(_failed_run(i + 40, tests=[failure]))
        with self.assertRaises(MinerDefect):
            MINER.run(self._ctx(runs))

    def test_empty_corpus_is_a_defect_not_a_clean_result(self):
        """The harvester never writes an empty corpus, so a zero-record file is
        damage -- and emitting nothing against it would read as 'no flaky tests'."""
        (self.out / "ci_failures.corpus.jsonl").write_text("", encoding="utf-8")
        ctx = MinerContext(
            root=self.root, commit="c", branch="main", options={"out_dir": str(self.out)}
        )
        with self.assertRaises(MinerDefect) as caught:
            MINER.run(ctx)
        self.assertIn("zero records", str(caught.exception))

    def test_missing_corpus_says_how_to_get_it(self):
        ctx = MinerContext(
            root=self.root, commit="c", branch="main", options={"out_dir": str(self.out)}
        )
        with self.assertRaises(OSError) as caught:
            MINER.run(ctx)
        self.assertIn("harvest --source ci_failures", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
