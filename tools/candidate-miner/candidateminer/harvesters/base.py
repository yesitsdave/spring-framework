"""Shared harvester types."""

from __future__ import annotations


class HarvestDefect(RuntimeError):
    """A harvester detected a fault in itself and refuses to emit.

    Mirrors `MinerDefect`. A corpus that is obviously wrong -- empty when it
    cannot be, or so broad it filters nothing -- must fail loudly rather than be
    handed downstream, where its emptiness or noise would be read as a finding
    about the repo instead of a bug in the tool.
    """
