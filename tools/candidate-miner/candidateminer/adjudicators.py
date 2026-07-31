"""Adjudicators: turn a packet into a verdict.

This is the **only** module in the project that talks to a model, and nothing
under `miners/` or `harvesters/` imports it — a test asserts that miner.py's
transitive import graph contains neither this module nor `anthropic`, so
"the miners are deterministic" is a checked property rather than a promise.

The model is deliberately given the narrowest possible job: look at one page's
evidence and decide whether the prose contradicts the code. It cannot author a
candidate's identity, cannot invent a claim type, and cannot cite a symbol it
was not shown — `adjudication.validate_verdict` rejects all three before
anything reaches the ledger.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from .adjudication import ClaimType, Packet, Verdict, VerdictKind

MODEL = "claude-opus-5"
PROMPT_VERSION = 1
_MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You adjudicate whether Spring Framework reference documentation has become \
factually wrong about the code it describes.

You will be given one documentation page's prose sections, the current public \
members of the types it documents, and the public signature changes since the \
page was last substantively edited.

Accept ONLY a contradiction: the documentation states something the code \
contradicts. Examples that qualify:
  - a rendered interface or class omits members it actually has
  - prose states a count ("adds two other methods") that is wrong
  - a shown signature no longer matches the real one
  - docs reference a member that no longer exists
  - a member is shown with the wrong modifier (e.g. abstract when it is default)

Reject everything else. In particular, REJECT:
  - omissions. Spring deliberately does not document every method. A new method \
that the page simply does not mention is NOT a contradiction.
  - anything merely dated, stylistic, or that "could be clearer".
  - anything you are not certain about. Uncertainty is a reject.

Rejecting is the correct answer most of the time. A wrong accept is far more \
costly than a missed finding: it wastes a maintainer's review attention, which \
is the scarcest resource on this project.

If you accept, you must:
  - cite a symbol that appears in the evidence you were given
  - choose a claim_type from the fixed list
  - give a citation as "path:line" pointing at the exact line that is wrong. \
The path must be the documentation page itself or one of the source files \
shown above; citations to any other file are discarded.
  - state the contradiction in one sentence

Reply only with the required JSON object."""

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": [k.value for k in VerdictKind],
            "description": "accept only for a genuine contradiction",
        },
        "symbol": {
            "type": "string",
            "description": "a symbol from the supplied evidence; empty when rejecting",
        },
        "claim_type": {
            "type": "string",
            "enum": [c.value for c in ClaimType] + [""],
            "description": "the kind of contradiction; empty when rejecting",
        },
        "rationale": {
            "type": "string",
            "description": "one sentence stating the contradiction",
        },
        "citation": {
            "type": "string",
            "description": "path:line of the wrong line; empty when rejecting",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["verdict", "symbol", "claim_type", "rationale", "citation", "confidence"],
    "additionalProperties": False,
}


class Adjudicator(Protocol):
    name: str

    def adjudicate(self, packet: Packet) -> Verdict:
        ...


class AdjudicatorError(RuntimeError):
    pass


def render_packet(packet: Packet) -> str:
    """The user-turn text. Deterministic, so it can be inspected without a model."""
    parts = [
        f"PAGE: {packet.path}",
        f"Docs last substantively edited: {packet.doc_last_date}",
        f"Referenced code last substantively changed: {packet.code_last_date}",
        "",
        "=== CURRENT PUBLIC MEMBERS OF THE DOCUMENTED TYPES ===",
    ]
    for summary in packet.types:
        members = ", ".join(summary.members) or "(none detected)"
        parts.append(f"{summary.kind} {summary.fqn}  [{summary.path}]")
        parts.append(f"    members: {members}")

    parts += ["", "=== PUBLIC SIGNATURE CHANGES SINCE THE PAGE WAS EDITED ==="]
    parts += [f"  {change}" for change in packet.signature_changes] or ["  (none)"]

    parts += ["", "=== DOCUMENTATION SECTIONS ==="]
    for heading, body in packet.sections:
        parts += [f"--- {heading} ---", body, ""]
    return "\n".join(parts)


@dataclass
class StubAdjudicator:
    """Deterministic stand-in used by the tests and by --dry-run.

    Keeps the whole pathway exercisable without a model, a key, or a network.
    """

    verdict: Verdict = None  # type: ignore[assignment]
    name: str = "stub"

    def adjudicate(self, packet: Packet) -> Verdict:
        return self.verdict or Verdict(kind=VerdictKind.REJECT, model="stub")


@dataclass
class ClaudeAdjudicator:
    model: str = MODEL
    name: str = "claude"
    _client: object = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            import anthropic  # imported lazily: miner.py must never need it
        except ImportError as exc:
            raise AdjudicatorError(
                "the anthropic SDK is required for adjudication but is not installed.\n"
                "  pip install anthropic\n"
                "  then authenticate: `ant auth login`, or set ANTHROPIC_API_KEY."
            ) from exc
        self._client = anthropic.Anthropic()
        return self._client

    def adjudicate(self, packet: Packet) -> Verdict:
        client = self._ensure_client()
        # The system prompt is identical for every packet, so cache it: with a
        # corpus of pages this turns into a cache read on all but the first call.
        response = client.messages.create(
            model=self.model,
            max_tokens=_MAX_TOKENS,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": _VERDICT_SCHEMA}},
            messages=[{"role": "user", "content": render_packet(packet)}],
        )

        # Safety classifiers can decline; content is empty or partial in that
        # case, so stop_reason must be checked before content is read.
        if response.stop_reason == "refusal":
            raise AdjudicatorError(
                f"model declined to adjudicate {packet.page} "
                f"(category: {getattr(response.stop_details, 'category', None)})"
            )

        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"), ""
        )
        return _parse_verdict(text, model=self.model)


def _parse_verdict(text: str, *, model: str) -> Verdict:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdjudicatorError(f"adjudicator returned non-JSON: {text[:200]!r}") from exc

    try:
        kind = VerdictKind(payload["verdict"])
    except (KeyError, ValueError) as exc:
        raise AdjudicatorError(f"unknown verdict: {payload.get('verdict')!r}") from exc

    claim_raw = (payload.get("claim_type") or "").strip()
    try:
        claim = ClaimType(claim_raw) if claim_raw else None
    except ValueError as exc:
        # The schema constrains this, but a schema is a request, not a guarantee.
        raise AdjudicatorError(f"unknown claim_type: {claim_raw!r}") from exc

    return Verdict(
        kind=kind,
        symbol=(payload.get("symbol") or "").strip(),
        claim_type=claim,
        rationale=(payload.get("rationale") or "").strip(),
        citation=(payload.get("citation") or "").strip(),
        confidence=(payload.get("confidence") or "medium").strip(),
        model=model,
        prompt_version=PROMPT_VERSION,
    )
