"""triage.py CLI — the full pathway, driven by a stub adjudicator."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import triage
from candidateminer.adjudication import ClaimType, Verdict, VerdictKind
from candidateminer.adjudicators import StubAdjudicator

_PAGE = "framework-docs/modules/ROOT/pages/core/beans/nature.adoc"


def _workspace() -> tuple[Path, Path]:
    """A repo with one documented interface, plus a matching docs_drift corpus."""
    root = Path(tempfile.mkdtemp())
    src = root / "spring-context/src/main/java/org/springframework/context"
    src.mkdir(parents=True)
    (src / "Lifecycle.java").write_text(
        "package org.springframework.context;\n"
        "public interface Lifecycle {\n\tvoid start();\n\tvoid stop();\n}\n",
        encoding="utf-8",
    )
    (src / "LifecycleProcessor.java").write_text(
        "package org.springframework.context;\n"
        "public interface LifecycleProcessor extends Lifecycle {\n"
        "\tdefault void onRefresh() {\n\t}\n\tdefault void onPause() {\n\t}\n}\n",
        encoding="utf-8",
    )
    page = root / _PAGE
    page.parent.mkdir(parents=True)
    page.write_text(
        "= Nature\n\n== Lifecycle\n\n"
        "See org.springframework.context.Lifecycle here.\n"
        "The LifecycleProcessor adds two other methods.\n",
        encoding="utf-8",
    )

    out = Path(tempfile.mkdtemp())
    record = {
        "page": "core/beans/nature.adoc",
        "path": _PAGE,
        "doc_last_substantive": {"commit": "abc", "date": "2024-01-01"},
        "code_last_substantive": {"date": "2026-01-01"},
        "referenced_types": ["org.springframework.context.Lifecycle"],
        "signature_changes": ["+\tdefault void onPause() {"],
    }
    (out / "docs_drift.corpus.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )
    return root, out


def _accepting(**overrides) -> StubAdjudicator:
    base = dict(
        kind=VerdictKind.ACCEPT,
        symbol="LifecycleProcessor",
        claim_type=ClaimType.WRONG_COUNT,
        rationale="Page says two methods; the interface declares more.",
        citation=f"{_PAGE}:6",
        confidence="high",
        model="stub",
    )
    base.update(overrides)
    return StubAdjudicator(verdict=Verdict(**base))


class TriageCliTests(unittest.TestCase):
    def setUp(self):
        self.root, self.out = _workspace()
        self.ledger = Path(tempfile.mkdtemp())

    def _cli(self, *argv, adjudicator: StubAdjudicator | None = None):
        args = [
            *argv,
            "--repo", str(self.root),
            "--out-dir", str(self.out),
        ]
        if argv[0] == "run":
            args += ["--ledger-dir", str(self.ledger), "--adjudicator", "stub"]
        stdout, stderr = io.StringIO(), io.StringIO()
        patch = (
            mock.patch.object(triage, "StubAdjudicator", lambda: adjudicator)
            if adjudicator
            else contextlib.nullcontext()
        )
        with patch, contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = triage.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_packet_calls_no_model(self):
        code, out, _ = self._cli("packet")
        self.assertEqual(code, 0)
        self.assertIn("1 packet(s)", out)
        self.assertIn("No model was called", out)

    def test_packet_show_renders_evidence(self):
        code, out, _ = self._cli("packet", "--show", "1")
        self.assertEqual(code, 0)
        self.assertIn("LifecycleProcessor", out)
        self.assertIn("onPause", out)
        self.assertIn("DOCUMENTATION SECTIONS", out)

    def test_packet_show_out_of_range_is_an_error(self):
        code, _, err = self._cli("packet", "--show", "5")
        self.assertEqual(code, triage.EXIT_ERROR)
        self.assertIn("--show must be", err)

    def test_default_stub_rejects_and_writes_nothing(self):
        code, out, _ = self._cli("run", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("0 accepted, 1 rejected", out)
        self.assertFalse((self.ledger / "adjudicated_docs_drift.jsonl").exists())

    def test_accepted_verdict_reaches_the_ledger(self):
        code, out, _ = self._cli("run", adjudicator=_accepting())
        self.assertEqual(code, 0)
        self.assertIn("1 accepted", out)
        ledger = self.ledger / "adjudicated_docs_drift.jsonl"
        self.assertTrue(ledger.is_file())
        entry = json.loads(ledger.read_text().splitlines()[0])
        self.assertEqual(entry["state"], "new")
        self.assertIn("LifecycleProcessor", entry["identity"])

    def test_unsupported_citation_is_discarded_not_recorded(self):
        """A claim the evidence can't support must never become a ledger entry."""
        code, out, _ = self._cli("run", adjudicator=_accepting(citation="nope.adoc:1"))
        self.assertEqual(code, 0)
        self.assertIn("DISCARD", out)
        self.assertIn("0 accepted", out)
        self.assertFalse((self.ledger / "adjudicated_docs_drift.jsonl").exists())

    def test_invented_symbol_is_discarded(self):
        code, out, _ = self._cli("run", adjudicator=_accepting(symbol="NotInEvidence"))
        self.assertIn("DISCARD", out)
        self.assertFalse((self.ledger / "adjudicated_docs_drift.jsonl").exists())

    def test_decline_survives_a_second_adjudication(self):
        """An adjudicated candidate declines exactly like a mined one."""
        from candidateminer.contract import State
        from candidateminer.ledger import Ledger

        self._cli("run", adjudicator=_accepting())
        path = self.ledger / "adjudicated_docs_drift.jsonl"
        ledger = Ledger.load(path)
        fingerprint = ledger.sorted_entries()[0].fingerprint
        ledger.set_state(fingerprint, State.DECLINED, reason="intentional")
        ledger.save(path)

        code, out, _ = self._cli("run", adjudicator=_accepting())
        self.assertEqual(code, 0)
        self.assertIn("1 suppressed by a previous decline", out)

    def test_missing_corpus_says_how_to_get_it(self):
        (self.out / "docs_drift.corpus.jsonl").unlink()
        code, _, err = self._cli("packet")
        self.assertEqual(code, triage.EXIT_ERROR)
        self.assertIn("harvest --source docs_drift", err)

    def test_unknown_corpus_is_a_different_error(self):
        code, _, err = self._cli("packet", "--source", "nope")
        self.assertEqual(code, triage.EXIT_ERROR)
        self.assertIn("unknown corpus", err)


class MissingSdkTests(unittest.TestCase):
    def test_live_run_without_the_sdk_explains_the_fix(self):
        from candidateminer.adjudicators import AdjudicatorError, ClaudeAdjudicator

        with mock.patch.dict("sys.modules", {"anthropic": None}):
            with self.assertRaises(AdjudicatorError) as caught:
                ClaudeAdjudicator()._ensure_client()
        self.assertIn("pip install anthropic", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
