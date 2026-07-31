"""Miner registry.

Miners are registered by name and grouped by cost class so later scheduling can
run API-only, source-scan and build-requiring miners on completely separate
cadences without any of them knowing about the others.
"""

from __future__ import annotations

from ..contract import CostClass
from .base import Miner, MinerContext, MinerDefect
from .config_dead_entry import MINER as _config_dead_entry
from .docs_dead_ref import MINER as _docs_dead_ref
from .flaky_test import MINER as _flaky_test

REGISTRY: dict[str, Miner] = {
    _config_dead_entry.name: _config_dead_entry,
    _docs_dead_ref.name: _docs_dead_ref,
    _flaky_test.name: _flaky_test,
}


def get(name: str) -> Miner:
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown miner {name!r}; known miners: {known}") from None


def by_cost_class(cost_class: CostClass) -> list[Miner]:
    return sorted(
        (m for m in REGISTRY.values() if m.cost_class is cost_class),
        key=lambda m: m.name,
    )


__all__ = [
    "REGISTRY",
    "Miner",
    "MinerContext",
    "MinerDefect",
    "get",
    "by_cost_class",
]
