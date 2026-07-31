"""Harvesters: deterministic corpus builders.

A harvester is not a miner. Miners emit scored `Candidate` records for triage;
harvesters emit a corpus that a *downstream* step reasons over.

The distinction is about what can be proved. `docs_dead_ref` can prove a reference
is dead -- the type is absent from the tree. Nothing deterministic can prove a
paragraph no longer describes its subject, or that a maintainer's review comment
encodes a general rule. Those need judgement, so the deterministic stage stops at
assembling the evidence and the ledger stays free of unverifiable items.

Harvesters declare a cost class like miners do; they are not all remote.
"""

from __future__ import annotations

from ..contract import CostClass
from . import ci_failures, docs_drift, review_comments

REGISTRY = {
    ci_failures.CORPUS: ci_failures,
    docs_drift.CORPUS: docs_drift,
    review_comments.CORPUS: review_comments,
}


def get(name: str):
    try:
        return REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown corpus {name!r}; known corpora: {known}") from None


def by_cost_class(cost_class: CostClass):
    return sorted(
        (m for m in REGISTRY.values() if m.COST_CLASS is cost_class),
        key=lambda m: m.CORPUS,
    )


__all__ = ["REGISTRY", "get", "by_cost_class", "ci_failures", "docs_drift", "review_comments"]
