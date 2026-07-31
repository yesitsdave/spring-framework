"""Deterministic scaffolding for the adjudication stage.

**No LLM here.** This module assembles the evidence a model needs, defines the
shape of an acceptable verdict, and turns an accepted verdict into an ordinary
`Candidate`. The model call itself lives in `triage.py`.

The design constraint that shapes everything below: **the model must not author a
candidate's identity.** The ledger depends on
`sha256(category ‖ path ‖ identity)` being reproducible, and a free-text identity
would change whenever the model rephrased itself — minting a second fingerprint for
the same finding, breaking dedupe, and silently un-suppressing a declined
candidate. That would destroy the one property the ledger exists to provide.

So a verdict must cite a **symbol** and pick a **claim type from a closed
vocabulary**. Identity is `{page}::{symbol}::{claim_type}`: stable across
rephrasing, and both halves are checkable before anything reaches the ledger — the
symbol must exist in the tree, the claim type must be in the enum.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .contract import Candidate, CostClass, Locus, Score, canonical_json
from .index import TypeIndex

CATEGORY = "docs.semantic_drift"
ADJUDICATOR_VERSION = "1.0.0"

_RE_HEADING = re.compile(r"^(=+)\s+(.*)$")
_RE_MEMBER = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:public|protected)\s+(?:default\s+|static\s+|final\s+|abstract\s+)*"
    r"[^;{=]*[\w>\]]\s+(\w+)\s*\("
)

# Interface members carry no access modifier -- `void start();`,
# `default void onPause() {`. Requiring `public` made every interface in a packet
# look empty, which would have hidden exactly the evidence the pilot finding
# rested on (`LifecycleProcessor` having four methods, not two).
_RE_INTERFACE_MEMBER = re.compile(
    r"^\t(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:public\s+|default\s+|static\s+|abstract\s+)*"
    r"[\w<>\[\], .?]+\s+(\w+)\s*\([^)]*\)\s*[;{]"
)
_RE_TYPE_DECL = re.compile(
    r"^\s*public\s+(?:abstract\s+|final\s+|sealed\s+)*"
    r"(class|interface|enum|record|@interface)\s+(\w+)"
)
_RE_CITATION = re.compile(r"^(?P<path>[\w./-]+):(?P<line>\d+)$")

_MAX_SECTION_CHARS = 6000
_MAX_MEMBERS = 40
_MAX_CHANGES = 25


class ClaimType(str, Enum):
    """Closed vocabulary. A verdict outside it is discarded, not interpreted."""

    INCOMPLETE_LISTING = "incomplete_listing"   # a rendered type omits members it has
    WRONG_COUNT = "wrong_count"                 # prose states a count that is wrong
    STALE_SIGNATURE = "stale_signature"         # a shown signature no longer matches
    REMOVED_MEMBER = "removed_member"           # docs reference a member that is gone
    WRONG_MODIFIER = "wrong_modifier"           # e.g. shown abstract, actually default


class VerdictKind(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


class VerdictError(ValueError):
    """A verdict is malformed or unsupported by the evidence."""


@dataclass(frozen=True)
class TypeSummary:
    fqn: str
    path: str
    kind: str
    members: tuple[str, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "fqn": self.fqn,
            "path": self.path,
            "kind": self.kind,
            "members": list(self.members),
        }


@dataclass(frozen=True)
class Packet:
    """Everything an adjudicator needs for one page, and nothing else."""

    page: str
    path: str
    doc_last_date: str
    code_last_date: str
    sections: tuple[tuple[str, str], ...]
    types: tuple[TypeSummary, ...]
    signature_changes: tuple[str, ...]

    @property
    def evidence_digest(self) -> str:
        """Hash of the evidence a verdict was formed against.

        Lets a verdict be cached against a fingerprint and reused until the
        underlying evidence actually changes.
        """
        payload = canonical_json(
            {
                "path": self.path,
                "sections": [list(s) for s in self.sections],
                "types": [t.to_json() for t in self.types],
                "changes": list(self.signature_changes),
            }
        )
        return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def symbols(self) -> frozenset[str]:
        """Simple names a verdict is allowed to cite."""
        names: set[str] = set()
        for summary in self.types:
            names.add(summary.fqn.rsplit(".", 1)[-1])
            names.update(summary.members)
        return frozenset(names)

    def citable_paths(self) -> frozenset[str]:
        """Files a verdict is allowed to cite: the page and the shown sources.

        Exact-match against these repo-relative paths is also what keeps a
        citation inside the repo -- there is no path arithmetic to traverse with.
        """
        return frozenset({self.path, *(t.path for t in self.types)})

    def to_json(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "path": self.path,
            "doc_last_date": self.doc_last_date,
            "code_last_date": self.code_last_date,
            "evidence_digest": self.evidence_digest,
            "sections": [{"heading": h, "text": t} for h, t in self.sections],
            "types": [t.to_json() for t in self.types],
            "signature_changes": list(self.signature_changes),
        }


@dataclass(frozen=True)
class Verdict:
    kind: VerdictKind
    symbol: str = ""
    claim_type: ClaimType | None = None
    rationale: str = ""
    citation: str = ""
    confidence: str = "medium"
    model: str = "unknown"
    prompt_version: int = 1

    def to_json(self) -> dict[str, Any]:
        return {
            "verdict": self.kind.value,
            "symbol": self.symbol,
            "claim_type": self.claim_type.value if self.claim_type else None,
            "rationale": self.rationale,
            "citation": self.citation,
            "confidence": self.confidence,
            "model": self.model,
            "prompt_version": self.prompt_version,
        }


def build_packet(root: Path, record: dict, index: TypeIndex) -> Packet:
    """Assemble one page's evidence. Deterministic."""
    path = record["path"]
    referenced = list(record.get("referenced_types", []))
    text = (root / path).read_text(encoding="utf-8", errors="replace")

    simple_names = {f.rsplit(".", 1)[-1] for f in referenced}
    sections = tuple(
        (heading, body[:_MAX_SECTION_CHARS])
        for heading, body in _split_sections(text)
        if any(name in body for name in simple_names)
    )

    for fqn in _sibling_types_named_in(text, referenced, index):
        if fqn not in referenced:
            referenced.append(fqn)

    summaries = []
    for fqn in sorted(referenced):
        summary = _summarise_type(root, fqn, index)
        if summary is not None:
            summaries.append(summary)

    return Packet(
        page=record["page"],
        path=path,
        doc_last_date=record["doc_last_substantive"]["date"],
        code_last_date=record["code_last_substantive"]["date"],
        sections=sections,
        types=tuple(summaries),
        signature_changes=tuple(record.get("signature_changes", [])[:_MAX_CHANGES]),
    )


def _split_sections(text: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, list[str]]] = [("(preamble)", [])]
    for line in text.splitlines():
        match = _RE_HEADING.match(line)
        if match:
            sections.append((match.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(body)) for heading, body in sections]


def _sibling_types_named_in(
    text: str, referenced: list[str], index: TypeIndex
) -> list[str]:
    """Types the page names by simple name, resolved within packages it already cites.

    Reference docs overwhelmingly write `LifecycleProcessor`, not the FQN, so the
    harvester's FQN-only capture under-collects. Resolution is scoped to packages
    already established as relevant by an FQN on the same page, which keeps a
    common simple name from dragging in an unrelated type.
    """
    packages = {fqn.rsplit(".", 1)[0] for fqn in referenced}
    if not packages:
        return []
    named = set(re.findall(r"\b([A-Z][A-Za-z0-9]{3,})\b", text))
    found = []
    for simple in sorted(named):
        matches = [
            f"{package}.{simple}"
            for package in sorted(packages)
            if f"{package}.{simple}" in index.main_fqns
        ]
        if len(matches) == 1:  # ambiguous across packages -> skip rather than guess
            found.append(matches[0])
    return found


def _summarise_type(root: Path, fqn: str, index: TypeIndex) -> TypeSummary | None:
    """The public surface of a type, as it exists right now.

    A member list rather than the whole file: it is what a prose claim is checked
    against, and it keeps the packet small enough to reason about.
    """
    rel = _source_path_for(root, fqn, index)
    if rel is None:
        return None
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    simple = fqn.rsplit(".", 1)[-1]
    lines = text.splitlines()

    kind = "type"
    for line in lines:
        declaration = _RE_TYPE_DECL.match(line)
        if declaration and declaration.group(2) == simple:
            kind = declaration.group(1)
            break

    # Interfaces need the modifier-less form; using it on classes would sweep in
    # method calls from method bodies.
    pattern = _RE_INTERFACE_MEMBER if kind == "interface" else _RE_MEMBER
    members = [m.group(1) for line in lines if (m := pattern.match(line))]
    ordered = sorted(dict.fromkeys(members))[:_MAX_MEMBERS]
    return TypeSummary(fqn=fqn, path=rel, kind=kind, members=tuple(ordered))


def _source_path_for(root: Path, fqn: str, index: TypeIndex) -> str | None:
    if fqn not in index.main_fqns:
        return None
    tail = fqn.replace(".", "/") + ".java"
    for candidate in sorted(root.glob(f"*/src/main/java/{tail}")):
        return candidate.relative_to(root).as_posix()
    return None


def validate_verdict(verdict: Verdict, packet: Packet, root: Path) -> None:
    """Reject anything the evidence does not support, before the ledger sees it.

    Every check here exists because the alternative is a confident, plausible,
    unverifiable claim entering a precision-first pipeline.
    """
    if verdict.kind is not VerdictKind.ACCEPT:
        return

    if verdict.claim_type is None:
        raise VerdictError("an accepted verdict must carry a claim_type")
    if not verdict.symbol:
        raise VerdictError("an accepted verdict must cite a symbol")
    if verdict.symbol not in packet.symbols():
        raise VerdictError(
            f"cited symbol {verdict.symbol!r} does not appear in the packet's "
            f"evidence; a verdict may only cite what it was shown"
        )
    if not verdict.rationale.strip():
        raise VerdictError("an accepted verdict must carry a rationale")

    match = _RE_CITATION.match(verdict.citation.strip())
    if not match:
        raise VerdictError(
            f"citation must be file:line, got {verdict.citation!r}. "
            f"Unciteable claims are discarded rather than trusted."
        )
    # Same rule as for symbols: a verdict may only cite what it was shown.
    # Checking mere existence let a citation name any file at all -- including,
    # via `..`, files outside the repo -- and a prompt-injected page could use
    # that to launder an arbitrary path into the emitted candidate.
    if match.group("path") not in packet.citable_paths():
        raise VerdictError(
            f"citation {match.group('path')!r} is not in the packet's evidence; "
            f"a verdict may only cite the page or a source file it was shown"
        )
    cited = root / match.group("path")
    if not cited.is_file():
        raise VerdictError(f"citation points at a missing file: {match.group('path')}")
    line = int(match.group("line"))
    total = len(cited.read_text(encoding="utf-8", errors="replace").splitlines())
    if not 1 <= line <= total:
        raise VerdictError(
            f"citation line {line} is outside {match.group('path')} (1-{total})"
        )


def candidate_from_verdict(
    packet: Packet, verdict: Verdict, *, commit: str = "unknown", branch: str = "unknown"
) -> Candidate:
    """Turn an accepted verdict into an ordinary ledger candidate."""
    if verdict.kind is not VerdictKind.ACCEPT:
        raise VerdictError("only an accepted verdict becomes a candidate")

    assert verdict.claim_type is not None  # guaranteed by validate_verdict
    identity = f"{packet.page}::{verdict.symbol}::{verdict.claim_type.value}"

    inputs = _score_inputs(verdict)
    evidence = {
        "reference": f"{verdict.symbol} ({verdict.claim_type.value})",
        "claim_type": verdict.claim_type.value,
        "symbol": verdict.symbol,
        "doc_last_date": packet.doc_last_date,
        "code_last_date": packet.code_last_date,
        "signature_changes": list(packet.signature_changes[:6]),
        "triage": {**verdict.to_json(), "evidence_digest": packet.evidence_digest},
    }

    return Candidate(
        category=CATEGORY,
        cost_class=CostClass.B_SOURCE,
        miner_name="adjudicated_docs_drift",
        miner_version=ADJUDICATOR_VERSION,
        locus=Locus(path=packet.path, line=_cited_line(verdict, packet)),
        identity=identity,
        evidence=evidence,
        score=Score(value=sum(inputs.values()), inputs=inputs),
        pr_grouping_key=packet.path,
        repo_commit=commit,
        repo_branch=branch,
    )


def _cited_line(verdict: Verdict, packet: Packet) -> int:
    # The locus is on the doc page, so only a citation *of the page* can supply
    # its line; a line into a cited .java source would be recorded against the
    # wrong file.
    match = _RE_CITATION.match(verdict.citation.strip())
    if match and match.group("path") == packet.path:
        return int(match.group("line"))
    return 1


# Claim types differ in how mechanically checkable a reviewer will find them.
# A wrong count is arithmetic; a stale signature needs a judgement call.
_CLAIM_PRIOR = {
    ClaimType.WRONG_COUNT: 40,
    ClaimType.INCOMPLETE_LISTING: 35,
    ClaimType.REMOVED_MEMBER: 35,
    ClaimType.WRONG_MODIFIER: 30,
    ClaimType.STALE_SIGNATURE: 25,
}

_CONFIDENCE_PRIOR = {"high": 20, "medium": 10, "low": 0}


def _score_inputs(verdict: Verdict) -> dict[str, int]:
    assert verdict.claim_type is not None
    return {
        "claim_type_prior": _CLAIM_PRIOR.get(verdict.claim_type, 20),
        "adjudicator_confidence": _CONFIDENCE_PRIOR.get(verdict.confidence, 0),
        "has_citation": 15,
        "docs_only_change": 10,
    }
