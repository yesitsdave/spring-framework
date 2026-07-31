"""The mining/triage contract: one JSONL record per candidate.

This is the interface between deterministic mining (here) and agentic triage
(downstream, later). Two properties matter most:

* **Content-addressed fingerprint.** Derived from *what* the candidate is, never
  from where it currently sits in a file. Line numbers drift constantly in an
  active repo; a fingerprint that drifts with them would make the ledger useless.
* **No wall-clock timestamps in records.** Run metadata lives in a separate
  manifest. Two runs against the same commit therefore produce byte-identical
  output, so `diff` between runs shows real change and nothing else.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1

_FP_SEP = "\x00"


class CostClass(str, Enum):
    """How expensive a miner is to run -- drives scheduling, kept separate from day one.

    Miners in different cost classes are never coupled: later workflows run them
    on different schedules.
    """

    A_API = "a_api"        # remote API only, no checkout
    B_SOURCE = "b_source"  # needs a checkout, no build
    C_BUILD = "c_build"    # needs a Gradle build


class State(str, Enum):
    NEW = "new"
    QUEUED = "queued"
    DONE = "done"
    DECLINED = "declined"  # terminal: suppressed permanently


TERMINAL_STATES = frozenset({State.DECLINED})


def canonical_json(obj: Any) -> str:
    """Stable serialisation: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True)
class Locus:
    path: str
    line: int
    kind: str = "file_span"
    # The line is where the evidence *currently* sits. It is advisory: it is not
    # part of the fingerprint and is expected to drift.
    line_is_advisory: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "path": self.path,
            "line": self.line,
            "line_is_advisory": self.line_is_advisory,
        }


@dataclass(frozen=True)
class Score:
    value: int
    inputs: dict[str, int]

    def to_json(self) -> dict[str, Any]:
        return {"value": self.value, "inputs": dict(sorted(self.inputs.items()))}


@dataclass(frozen=True)
class Candidate:
    """One mined candidate.

    ``identity`` supplies the fingerprint inputs. For the docs miner it is the
    dead reference itself, so the fingerprint survives arbitrary line movement
    and is unaffected by how many times the reference appears in the file.
    """

    category: str
    cost_class: CostClass
    miner_name: str
    miner_version: str
    locus: Locus
    identity: str
    evidence: dict[str, Any]
    score: Score
    pr_grouping_key: str
    repo_commit: str = "unknown"
    repo_branch: str = "unknown"

    @property
    def fingerprint(self) -> str:
        payload = _FP_SEP.join((self.category, self.locus.path, self.identity))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "fingerprint": self.fingerprint,
            "category": self.category,
            "cost_class": self.cost_class.value,
            "miner": {"name": self.miner_name, "version": self.miner_version},
            "repo": {"commit": self.repo_commit, "branch": self.repo_branch},
            "locus": self.locus.to_json(),
            "identity": self.identity,
            "evidence": self.evidence,
            "pr_grouping_key": self.pr_grouping_key,
            "score": self.score.to_json(),
        }

    def to_jsonl(self) -> str:
        return canonical_json(self.to_record())


_REQUIRED_TOP_LEVEL = (
    "schema_version",
    "fingerprint",
    "category",
    "cost_class",
    "miner",
    "repo",
    "locus",
    "identity",
    "evidence",
    "pr_grouping_key",
    "score",
)


class ContractError(ValueError):
    """A record does not satisfy the contract."""


def validate_record(record: dict[str, Any]) -> None:
    """Raise :class:`ContractError` if ``record`` violates the contract."""
    for key in _REQUIRED_TOP_LEVEL:
        if key not in record:
            raise ContractError(f"missing required field: {key}")

    if record["schema_version"] != SCHEMA_VERSION:
        raise ContractError(
            f"unsupported schema_version {record['schema_version']!r} "
            f"(expected {SCHEMA_VERSION})"
        )

    fp = record["fingerprint"]
    if not isinstance(fp, str) or not fp.startswith("sha256:") or len(fp) != 71:
        raise ContractError(f"malformed fingerprint: {fp!r}")

    try:
        CostClass(record["cost_class"])
    except ValueError as exc:
        raise ContractError(f"unknown cost_class: {record['cost_class']!r}") from exc

    locus = record["locus"]
    if not isinstance(locus, dict) or "path" not in locus or "line" not in locus:
        raise ContractError("locus must carry both path and line")
    if not isinstance(locus["line"], int) or locus["line"] < 1:
        raise ContractError(f"locus.line must be a positive int, got {locus['line']!r}")

    score = record["score"]
    if not isinstance(score, dict) or "value" not in score or "inputs" not in score:
        raise ContractError("score must carry both value and inputs")
    if not isinstance(score["value"], int):
        # Deliberately integer-only: floats would vary across platforms and break
        # the byte-identical-output guarantee.
        raise ContractError(f"score.value must be an int, got {score['value']!r}")
    if not isinstance(score["inputs"], dict) or not score["inputs"]:
        raise ContractError("score.inputs must be a non-empty mapping")
    if sum(score["inputs"].values()) != score["value"]:
        raise ContractError(
            "score.value must equal the sum of its inputs "
            f"({score['value']} != {sum(score['inputs'].values())})"
        )
