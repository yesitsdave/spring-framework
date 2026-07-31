"""Persistent candidate state, so repeat runs diff rather than re-propose.

The load-bearing rule: **a human declining a candidate suppresses it permanently.**
Everything else here exists to make that rule hold across runs, across commits,
and across ephemeral CI runners.

This module never touches git. It reads and writes one JSONL file; committing it
is the caller's business. That keeps the miner testable offline and leaves the
choice of CI convention to the workflow layer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable, Sequence

from .contract import Candidate, State, canonical_json


@dataclass(frozen=True)
class LedgerEntry:
    fingerprint: str
    state: State
    category: str
    path: str
    identity: str
    first_seen_commit: str
    last_seen_commit: str
    reason: str | None = None
    decided_by: str | None = None

    def to_json(self) -> dict:
        record = {
            "fingerprint": self.fingerprint,
            "state": self.state.value,
            "category": self.category,
            "path": self.path,
            "identity": self.identity,
            "first_seen_commit": self.first_seen_commit,
            "last_seen_commit": self.last_seen_commit,
        }
        if self.reason is not None:
            record["reason"] = self.reason
        if self.decided_by is not None:
            record["decided_by"] = self.decided_by
        return record

    @classmethod
    def from_json(cls, record: dict) -> "LedgerEntry":
        return cls(
            fingerprint=record["fingerprint"],
            state=State(record["state"]),
            category=record["category"],
            path=record["path"],
            identity=record["identity"],
            first_seen_commit=record.get("first_seen_commit", "unknown"),
            last_seen_commit=record.get("last_seen_commit", "unknown"),
            reason=record.get("reason"),
            decided_by=record.get("decided_by"),
        )


@dataclass
class MergeResult:
    emitted: list[Candidate] = field(default_factory=list)
    newly_seen: list[str] = field(default_factory=list)
    suppressed: list[str] = field(default_factory=list)
    orphans: list[LedgerEntry] = field(default_factory=list)
    vanished: list[LedgerEntry] = field(default_factory=list)


class LedgerError(RuntimeError):
    pass


class Ledger:
    def __init__(self, entries: Iterable[LedgerEntry] = ()) -> None:
        self._entries: dict[str, LedgerEntry] = {e.fingerprint: e for e in entries}

    # -- persistence -------------------------------------------------------

    @classmethod
    def load(cls, path: Path) -> "Ledger":
        if not path.exists():
            return cls()
        entries = []
        for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line:
                continue
            try:
                entries.append(LedgerEntry.from_json(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                raise LedgerError(f"{path}:{lineno}: corrupt ledger entry: {exc}") from exc
        return cls(entries)

    def save(self, path: Path) -> bool:
        """Write the ledger. Returns True only if the file's bytes changed.

        The no-op-on-unchanged behaviour is what keeps a scheduled job from
        committing an identical file on every run.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = "".join(
            canonical_json(e.to_json()) + "\n" for e in self.sorted_entries()
        )
        if path.exists() and path.read_text(encoding="utf-8") == payload:
            return False
        path.write_text(payload, encoding="utf-8")
        return True

    def sorted_entries(self) -> list[LedgerEntry]:
        return [self._entries[k] for k in sorted(self._entries)]

    # -- queries -----------------------------------------------------------

    def get(self, fingerprint: str) -> LedgerEntry | None:
        return self._entries.get(fingerprint)

    def by_state(self, state: State) -> list[LedgerEntry]:
        return [e for e in self.sorted_entries() if e.state is state]

    def __len__(self) -> int:
        return len(self._entries)

    # -- mutation ----------------------------------------------------------

    def set_state(
        self,
        fingerprint: str,
        state: State,
        *,
        reason: str | None = None,
        decided_by: str | None = None,
    ) -> LedgerEntry:
        entry = self._entries.get(fingerprint)
        if entry is None:
            raise LedgerError(f"unknown fingerprint: {fingerprint}")
        if entry.state is State.DECLINED and state is not State.DECLINED:
            raise LedgerError(
                f"{fingerprint} is declined; that is terminal and cannot be reopened "
                f"by the tool. Edit the ledger by hand if this is truly intended."
            )
        if state is State.DECLINED and not reason:
            raise LedgerError("declining a candidate requires a reason")
        updated = replace(
            entry,
            state=state,
            reason=reason if reason is not None else entry.reason,
            decided_by=decided_by if decided_by is not None else entry.decided_by,
        )
        self._entries[fingerprint] = updated
        return updated

    def merge(
        self,
        candidates: Sequence[Candidate],
        *,
        commit: str,
        root: Path,
    ) -> MergeResult:
        """Fold a run's candidates into the ledger.

        Existing state always wins over a fresh sighting -- that is the whole
        point. Declined candidates are dropped from the emitted set.
        """
        result = MergeResult()
        seen: set[str] = set()

        for candidate in sorted(candidates, key=lambda c: c.fingerprint):
            fingerprint = candidate.fingerprint
            seen.add(fingerprint)
            existing = self._entries.get(fingerprint)

            if existing is None:
                self._entries[fingerprint] = LedgerEntry(
                    fingerprint=fingerprint,
                    state=State.NEW,
                    category=candidate.category,
                    path=candidate.locus.path,
                    identity=candidate.identity,
                    first_seen_commit=commit,
                    last_seen_commit=commit,
                )
                result.newly_seen.append(fingerprint)
                result.emitted.append(candidate)
                continue

            self._entries[fingerprint] = replace(existing, last_seen_commit=commit)
            if existing.state is State.DECLINED:
                result.suppressed.append(fingerprint)
            else:
                result.emitted.append(candidate)

        # Entries the run did not re-find. Two very different situations, and
        # conflating them would either hide a rename or cry wolf on a real fix.
        for entry in self.sorted_entries():
            if entry.fingerprint in seen or entry.state is State.NEW:
                continue
            if (root / entry.path).exists():
                result.vanished.append(entry)   # the reference was genuinely fixed
            else:
                result.orphans.append(entry)    # the file moved: needs a human

        return result
