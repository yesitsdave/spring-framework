"""Read-only git queries.

The miner never *writes* git state -- no commits, no branches, no index changes.
It reads history purely as evidence (when was this type deleted?). Every function
degrades gracefully to ``None`` when git is unavailable or the query finds nothing,
so the miner still runs against a plain directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_TIMEOUT = 30


def _run(root: Path, *args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = proc.stdout.strip()
    return out or None


def head_commit(root: Path) -> str:
    return _run(root, "rev-parse", "HEAD") or "unknown"


def current_branch(root: Path) -> str:
    return _run(root, "rev-parse", "--abbrev-ref", "HEAD") or "unknown"


def head_date(root: Path) -> str | None:
    """Commit date of HEAD as ``YYYY-MM-DD``.

    Used as "now" for age arithmetic. Wall-clock time would make scores change
    between runs against the same commit and break byte-identical output.
    """
    return _run(root, "log", "-1", "--format=%ad", "--date=short")


def last_deletion_of(root: Path, simple_name: str) -> tuple[str, str] | None:
    """Most recent commit that deleted a file named ``simple_name``.

    Returns ``(commit_sha, iso_date)`` or ``None``. This is what lets a candidate
    say "removed 2021-09-17 in 3c8724ba3d" rather than merely "not found".
    """
    if not simple_name or not simple_name.isidentifier():
        return None
    pathspecs = [f"*/{simple_name}.{ext}" for ext in ("java", "kt", "aj")]
    out = _run(
        root,
        "log",
        "-1",
        "--diff-filter=D",
        "--format=%H\t%ad",
        "--date=short",
        "--",
        *pathspecs,
    )
    if not out:
        return None
    first = out.splitlines()[0]
    if "\t" not in first:
        return None
    sha, _, date = first.partition("\t")
    return sha.strip(), date.strip()
