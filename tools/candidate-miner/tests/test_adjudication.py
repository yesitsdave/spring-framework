"""Adjudication scaffolding: packets, verdict validation, candidate construction.

The validation tests are the safety-critical ones. Every rejection case here
exists to stop a confident, plausible, unverifiable claim from reaching the
ledger — which in a precision-first pipeline is the expensive failure.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from candidateminer.adjudication import (
    ClaimType,
    Packet,
    TypeSummary,
    Verdict,
    VerdictError,
    VerdictKind,
    build_packet,
    candidate_from_verdict,
    validate_verdict,
)
from candidateminer.adjudicators import _parse_verdict, render_packet
from candidateminer.contract import validate_record
from candidateminer.index import TypeIndex

_HERE = Path(__file__).resolve().parent


def _tree() -> Path:
    root = Path(tempfile.mkdtemp())
    src = root / "spring-context/src/main/java/org/springframework/context"
    src.mkdir(parents=True)
    (src / "LifecycleProcessor.java").write_text(
        "package org.springframework.context;\n"
        "public interface LifecycleProcessor extends Lifecycle {\n"
        "\tdefault void onRefresh() {\n\t}\n"
        "\tdefault void onPause() {\n\t}\n"
        "\tdefault void onClose() {\n\t}\n"
        "}\n",
        encoding="utf-8",
    )
    (src / "Lifecycle.java").write_text(
        "package org.springframework.context;\n"
        "public interface Lifecycle {\n\tvoid start();\n\tvoid stop();\n}\n",
        encoding="utf-8",
    )
    page = root / "framework-docs/modules/ROOT/pages/core/beans/nature.adoc"
    page.parent.mkdir(parents=True)
    page.write_text(
        "= Nature\n\n"
        "== Lifecycle\n\n"
        "See org.springframework.context.Lifecycle for details.\n"
        "The LifecycleProcessor adds two other methods.\n",
        encoding="utf-8",
    )
    return root


def _record() -> dict:
    return {
        "page": "core/beans/nature.adoc",
        "path": "framework-docs/modules/ROOT/pages/core/beans/nature.adoc",
        "doc_last_substantive": {"commit": "abc", "date": "2024-01-01"},
        "code_last_substantive": {"date": "2026-01-01"},
        "referenced_types": ["org.springframework.context.Lifecycle"],
        "signature_changes": ["+\tdefault void onPause() {"],
    }


def _packet(root: Path) -> Packet:
    return build_packet(root, _record(), TypeIndex.build(root))


def _accept(**overrides) -> Verdict:
    base = dict(
        kind=VerdictKind.ACCEPT,
        symbol="LifecycleProcessor",
        claim_type=ClaimType.WRONG_COUNT,
        rationale="The page says two methods; the interface has three.",
        citation="framework-docs/modules/ROOT/pages/core/beans/nature.adoc:6",
        confidence="high",
    )
    base.update(overrides)
    return Verdict(**base)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.packet = _packet(self.root)

    def test_pulls_in_types_named_only_by_simple_name(self):
        """Docs write `LifecycleProcessor`, not the FQN -- it must still appear."""
        names = {t.fqn for t in self.packet.types}
        self.assertIn("org.springframework.context.LifecycleProcessor", names)

    def test_interface_members_are_captured(self):
        """Interface methods carry no `public`; requiring it emptied every interface."""
        processor = next(
            t for t in self.packet.types if t.fqn.endswith("LifecycleProcessor")
        )
        self.assertEqual(set(processor.members), {"onRefresh", "onPause", "onClose"})

    def test_digest_is_stable_and_evidence_sensitive(self):
        self.assertEqual(self.packet.evidence_digest, _packet(self.root).evidence_digest)
        changed = Packet(
            page=self.packet.page,
            path=self.packet.path,
            doc_last_date=self.packet.doc_last_date,
            code_last_date=self.packet.code_last_date,
            sections=self.packet.sections,
            types=self.packet.types + (TypeSummary("x.Y", "p", "class", ()),),
            signature_changes=self.packet.signature_changes,
        )
        self.assertNotEqual(self.packet.evidence_digest, changed.evidence_digest)

    def test_render_is_deterministic(self):
        self.assertEqual(render_packet(self.packet), render_packet(self.packet))


class VerdictValidationTests(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.packet = _packet(self.root)

    def test_accepts_a_well_formed_verdict(self):
        validate_verdict(_accept(), self.packet, self.root)

    def test_rejection_needs_no_evidence(self):
        validate_verdict(Verdict(kind=VerdictKind.REJECT), self.packet, self.root)

    def test_symbol_must_appear_in_the_packet(self):
        """The model may only cite what it was shown -- no outside knowledge."""
        with self.assertRaises(VerdictError) as caught:
            validate_verdict(_accept(symbol="TotallyMadeUp"), self.packet, self.root)
        self.assertIn("does not appear in the packet", str(caught.exception))

    def test_claim_type_is_required(self):
        with self.assertRaises(VerdictError):
            validate_verdict(_accept(claim_type=None), self.packet, self.root)

    def test_rationale_is_required(self):
        with self.assertRaises(VerdictError):
            validate_verdict(_accept(rationale="  "), self.packet, self.root)

    def test_citation_must_be_file_colon_line(self):
        with self.assertRaises(VerdictError) as caught:
            validate_verdict(_accept(citation="somewhere in the docs"), self.packet, self.root)
        self.assertIn("file:line", str(caught.exception))

    def test_citation_file_must_exist(self):
        with self.assertRaises(VerdictError):
            validate_verdict(_accept(citation="no/such/file.adoc:3"), self.packet, self.root)

    def test_citation_must_be_within_the_packets_evidence(self):
        """The regression: existence was the only check, so a verdict could cite
        any file at all and launder its path into the emitted candidate."""
        (self.root / "README.md").write_text("not shown to the model\n", encoding="utf-8")
        with self.assertRaises(VerdictError) as caught:
            validate_verdict(_accept(citation="README.md:1"), self.packet, self.root)
        self.assertIn("packet's evidence", str(caught.exception))

    def test_citation_cannot_traverse_outside_the_repo(self):
        outside = self.root.parent / "outside-the-repo.txt"
        outside.write_text("secret\n", encoding="utf-8")
        try:
            with self.assertRaises(VerdictError) as caught:
                validate_verdict(
                    _accept(citation="../outside-the-repo.txt:1"), self.packet, self.root
                )
            self.assertIn("packet's evidence", str(caught.exception))
        finally:
            outside.unlink()

    def test_citation_of_a_shown_source_file_is_allowed(self):
        shown = next(
            t.path for t in self.packet.types if t.fqn.endswith("LifecycleProcessor")
        )
        validate_verdict(_accept(citation=f"{shown}:2"), self.packet, self.root)

    def test_citation_line_must_be_in_range(self):
        with self.assertRaises(VerdictError) as caught:
            validate_verdict(
                _accept(citation=f"{self.packet.path}:9999"), self.packet, self.root
            )
        self.assertIn("outside", str(caught.exception))


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.root = _tree()
        self.packet = _packet(self.root)

    def test_identity_is_not_authored_by_the_model(self):
        """Rephrasing must not mint a second fingerprint for the same finding.

        If it did, a human decline would stop suppressing and the ledger's one
        guarantee would be gone.
        """
        first = candidate_from_verdict(self.packet, _accept())
        second = candidate_from_verdict(
            self.packet,
            _accept(
                rationale="Completely different wording for the very same defect.",
                citation=f"{self.packet.path}:5",
                confidence="low",
            ),
        )
        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_identity_distinguishes_symbol_and_claim(self):
        base = candidate_from_verdict(self.packet, _accept())
        other_symbol = candidate_from_verdict(self.packet, _accept(symbol="Lifecycle"))
        other_claim = candidate_from_verdict(
            self.packet, _accept(claim_type=ClaimType.INCOMPLETE_LISTING)
        )
        self.assertNotEqual(base.fingerprint, other_symbol.fingerprint)
        self.assertNotEqual(base.fingerprint, other_claim.fingerprint)

    def test_record_satisfies_the_contract(self):
        validate_record(candidate_from_verdict(self.packet, _accept()).to_record())

    def test_carries_the_evidence_digest_for_caching(self):
        candidate = candidate_from_verdict(self.packet, _accept())
        self.assertEqual(
            candidate.evidence["triage"]["evidence_digest"], self.packet.evidence_digest
        )

    def test_rejected_verdict_cannot_become_a_candidate(self):
        with self.assertRaises(VerdictError):
            candidate_from_verdict(self.packet, Verdict(kind=VerdictKind.REJECT))

    def test_locus_points_at_the_cited_line(self):
        candidate = candidate_from_verdict(self.packet, _accept())
        self.assertEqual(candidate.locus.line, 6)

    def test_source_file_citation_does_not_supply_the_page_locus_line(self):
        """The locus is on the doc page; a line into a cited .java file would be
        recorded against the wrong file."""
        shown = next(
            t.path for t in self.packet.types if t.fqn.endswith("LifecycleProcessor")
        )
        candidate = candidate_from_verdict(self.packet, _accept(citation=f"{shown}:2"))
        self.assertEqual(candidate.locus.path, self.packet.path)
        self.assertEqual(candidate.locus.line, 1)


class ParseTests(unittest.TestCase):
    def test_parses_a_well_formed_reply(self):
        verdict = _parse_verdict(
            '{"verdict":"accept","symbol":"X","claim_type":"wrong_count",'
            '"rationale":"r","citation":"a:1","confidence":"high"}',
            model="m",
        )
        self.assertIs(verdict.kind, VerdictKind.ACCEPT)
        self.assertIs(verdict.claim_type, ClaimType.WRONG_COUNT)

    def test_rejects_a_claim_type_outside_the_vocabulary(self):
        """The schema constrains this, but a schema is a request, not a guarantee."""
        from candidateminer.adjudicators import AdjudicatorError

        with self.assertRaises(AdjudicatorError):
            _parse_verdict(
                '{"verdict":"accept","symbol":"X","claim_type":"vibes_are_off",'
                '"rationale":"r","citation":"a:1","confidence":"high"}',
                model="m",
            )

    def test_rejects_non_json(self):
        from candidateminer.adjudicators import AdjudicatorError

        with self.assertRaises(AdjudicatorError):
            _parse_verdict("Sure! Here is my answer:", model="m")


class DeterminismBoundaryTests(unittest.TestCase):
    """`miner.py` must not be able to reach a model, even transitively."""

    def test_miner_imports_nothing_llm(self):
        probe = (
            "import sys; sys.path.insert(0, %r); import miner; "
            "bad=[m for m in sys.modules if m=='anthropic' "
            "or m.endswith('adjudicators') or m.endswith('adjudication')]; "
            "print(','.join(bad))" % str(_HERE.parent)
        )
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(
            out.stdout.strip(),
            "",
            "miner.py reached an LLM-capable module; the deterministic boundary leaked",
        )


if __name__ == "__main__":
    unittest.main()
