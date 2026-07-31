"""The uniform miner interface.

Every miner is independently runnable and declares its cost class, so later
scheduling can treat API-only, source-scan and build-requiring miners completely
separately. Nothing in this module knows about any specific miner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Iterable, Protocol, runtime_checkable

from ..contract import Candidate, CostClass
from ..index import TypeIndex


class MinerDefect(RuntimeError):
    """The miner detected a fault in *itself* and refuses to emit results.

    This exists because of a real incident during Phase 1 recon: an extractor
    reported 227 of 227 references unresolved. That was a URL-normalisation bug,
    not 227 findings. A precision-first tool must fail loudly on an implausible
    result rather than emit a wall of false positives that look like work.
    """


@dataclass
class MinerContext:
    root: Path
    commit: str = "unknown"
    branch: str = "unknown"
    options: dict[str, Any] = field(default_factory=dict)

    @cached_property
    def type_index(self) -> TypeIndex:
        """Built once and shared: walking the tree is the expensive part."""
        return TypeIndex.build(self.root)


@runtime_checkable
class Miner(Protocol):
    name: str
    cost_class: CostClass
    version: str
    description: str

    def run(self, ctx: MinerContext) -> Iterable[Candidate]:
        ...
