"""Reference-doc pages whose documented types have since changed public API.

Spec C, deterministic stage. **No LLM here.** This produces a reading list; deciding
whether the prose is actually wrong is a downstream judgement.

Why this is a harvester and not a miner: `docs_dead_ref` can prove a reference is
dead — the type is absent from the tree, full stop. Nothing deterministic can prove
that a *paragraph* no longer describes its subject. The honest deterministic output
is "this page documents classes that gained or lost public API since the page was
last substantively edited", which is a strong reason to read but not a fix. Emitting
that as a scored candidate would put unverifiable items in the ledger.

Two filters do the work, and the first was learned the hard way:

* **Bulk commits are excluded.** Using each file's raw last-commit date flagged 22
  pages that all shared one date -- a repo-wide docs sweep. That is the same trap as
  the PR `updated_at` reset, and the second time a naive recency signal has proved
  untrustworthy here. Commits touching more than 50 files are ignored on both sides.
* **A public signature must have changed.** Any commit at all is far too weak;
  internal refactors do not invalidate prose. Restricting to added or removed
  `public`/`protected` member declarations takes 20 pages to 14 and gives a
  meaningful ranking.

470 pages -> 14. That reduction ratio is the whole justification for a downstream
reading stage.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contract import CostClass
from .base import HarvestDefect

CORPUS = "docs_drift"
COST_CLASS = CostClass.B_SOURCE
DESCRIPTION = "doc pages whose documented types gained or lost public API since"
SCHEMA_VERSION = 1

PAGES_ROOT = "framework-docs/modules/ROOT/pages"

# A commit touching more than this many files is a sweep, not a change of meaning.
BULK_COMMIT_THRESHOLD = 50

# Below this a page barely references the framework and the signal is noise.
MIN_REFERENCED_TYPES = 3

_HISTORY_SINCE = "2022-01-01"
_MAX_PATHSPEC_FILES = 40
_MAX_RECORDED_CHANGES = 25
_MAX_RECORDED_TYPES = 40
_GIT_TIMEOUT = 120

_RE_FQN = re.compile(r"org\.springframework\.[A-Za-z0-9_.]*[A-Z][A-Za-z0-9_]*")
# Leading annotations must be tolerated. Matching only a bare `public`/`protected`
# captured one side of an `@Override`-adding diff and not the other, leaving an
# uncancellable `-` line that read as drift where nothing had changed.
_RE_SIGNATURE = re.compile(
    r"^[+-]\s*(?:@\w+(?:\([^)]*\))?\s+)*(?:public|protected)\s[^;{]*\("
)

# Annotations whose addition or removal does not change what a method does, so a
# signature differing only by these is not evidence that documentation went stale.
_RE_ANNOTATION = re.compile(
    r"@(?:Nullable|NonNull|NullMarked|NullUnmarked|Override|SafeVarargs"
    r"|Contract\([^)]*\)|Deprecated(?:\([^)]*\))?|SuppressWarnings\([^)]*\))\s*"
)
_RE_FILES_CHANGED = re.compile(r"(\d+) files? changed")


@dataclass(frozen=True)
class DriftedPage:
    page: str
    path: str
    doc_last_commit: str
    doc_last_date: str
    code_last_date: str
    referenced_types: tuple[str, ...]
    referenced_type_count: int
    signature_change_count: int
    signature_changes: tuple[str, ...]
    files_examined: int
    files_truncated: bool

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "corpus": CORPUS,
            "page": self.page,
            "path": self.path,
            "doc_last_substantive": {
                "commit": self.doc_last_commit,
                "date": self.doc_last_date,
            },
            "code_last_substantive": {"date": self.code_last_date},
            "referenced_types": list(self.referenced_types),
            "referenced_type_count": self.referenced_type_count,
            "signature_change_count": self.signature_change_count,
            "signature_changes": list(self.signature_changes),
            "files_examined": self.files_examined,
            "files_truncated": self.files_truncated,
        }


@dataclass
class HarvestStats:
    pages_scanned: int = 0
    pages_with_enough_refs: int = 0
    pages_code_moved_on: int = 0
    pages_with_signature_change: int = 0
    bulk_commits_excluded: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "pages_scanned": self.pages_scanned,
            "pages_with_enough_refs": self.pages_with_enough_refs,
            "pages_code_moved_on": self.pages_code_moved_on,
            "pages_with_signature_change": self.pages_with_signature_change,
            "bulk_commits_excluded": self.bulk_commits_excluded,
        }


def harvest(root: Path, *, limit: int = 0) -> tuple[list[DriftedPage], HarvestStats]:
    stats = HarvestStats()
    bulk, dates = _commit_index(root)
    stats.bulk_commits_excluded = len(bulk)
    fqn_to_file = _main_source_index(root)

    found: list[DriftedPage] = []
    pages_root = root / PAGES_ROOT
    for page in sorted(pages_root.rglob("*.adoc")) if pages_root.is_dir() else []:
        stats.pages_scanned += 1
        text = page.read_text(encoding="utf-8", errors="replace")
        referenced = sorted({m for m in _RE_FQN.findall(text) if m in fqn_to_file})
        if len(referenced) < MIN_REFERENCED_TYPES:
            continue
        stats.pages_with_enough_refs += 1

        files = [fqn_to_file[f] for f in referenced]
        truncated = len(files) > _MAX_PATHSPEC_FILES
        files = files[:_MAX_PATHSPEC_FILES]

        doc_commit, doc_date = _last_substantive(root, [str(page.relative_to(root))], bulk, dates)
        _, code_date = _last_substantive(root, files, bulk, dates)
        if not (doc_date and code_date and code_date > doc_date):
            continue
        stats.pages_code_moved_on += 1

        changes = _signature_changes(root, files, doc_commit) if doc_commit else []
        if not changes:
            continue
        stats.pages_with_signature_change += 1

        found.append(
            DriftedPage(
                page=str(page.relative_to(pages_root)),
                path=str(page.relative_to(root)),
                doc_last_commit=doc_commit or "",
                doc_last_date=doc_date,
                code_last_date=code_date,
                referenced_types=tuple(referenced[:_MAX_RECORDED_TYPES]),
                referenced_type_count=len(referenced),
                signature_change_count=len(changes),
                signature_changes=tuple(c[:200] for c in changes[:_MAX_RECORDED_CHANGES]),
                files_examined=len(files),
                files_truncated=truncated,
            )
        )

    _self_check(stats)
    ordered = sorted(found, key=lambda p: (-p.signature_change_count, p.path))
    return (ordered[:limit] if limit else ordered), stats


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_GIT_TIMEOUT,
            check=False,
            # Pinned locale: the `--shortstat` parse below matches the English
            # "files changed" line, and a localized git would silently turn the
            # bulk-commit filter into a no-op -- same corpus, different machine.
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _commit_index(root: Path) -> tuple[set[str], dict[str, str]]:
    """Map every recent commit to its date, and flag the sweeps.

    `--shortstat` gives the changed-file count without listing the files, which
    keeps this to a single cheap pass over history. Commits older than
    ``_HISTORY_SINCE`` are not in this index; ``_resolve_commit`` fills them in
    lazily so long-untouched pages are dated rather than dropped.
    """
    bulk: set[str] = set()
    dates: dict[str, str] = {}
    current: str | None = None
    out = _git(
        root,
        "log",
        f"--since={_HISTORY_SINCE}",
        "--format=__C__%H %ad",
        "--date=short",
        "--shortstat",
    )
    for line in out.splitlines():
        if line.startswith("__C__"):
            current, _, date = line[5:].partition(" ")
            dates[current] = date.strip()
        elif current and "files changed" in line:
            match = _RE_FILES_CHANGED.search(line)
            if match and int(match.group(1)) > BULK_COMMIT_THRESHOLD:
                bulk.add(current)
    return bulk, dates


def _main_source_index(root: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for rel in _git(root, "ls-files", "*/src/main/java/org/springframework/*.java").splitlines():
        _, _, tail = rel.partition("src/main/java/")
        if tail.endswith(".java"):
            index[tail[:-5].replace("/", ".")] = rel
    return index


def _last_substantive(
    root: Path, paths: list[str], bulk: set[str], dates: dict[str, str]
) -> tuple[str | None, str | None]:
    out = _git(root, "log", "-n", "25", "--format=%H", "--", *paths)
    for sha in out.split():
        date = _resolve_commit(root, sha, bulk, dates)
        if date:
            return sha, date
    return None, None


def _resolve_commit(
    root: Path, sha: str, bulk: set[str], dates: dict[str, str]
) -> str | None:
    """Date for a non-bulk commit; None when the commit is a bulk sweep.

    Commits inside the ``_HISTORY_SINCE`` window come from the single-pass
    index. Older ones are resolved lazily here -- previously they fell through
    ``dates.get(sha)`` as None and their pages were silently dropped, which
    excluded exactly the longest-untouched (highest-drift) pages. The lazy path
    applies the same bulk-sweep check, so both filters keep working on both
    sides of the window boundary.
    """
    if sha not in dates:
        out = _git(root, "show", "--format=__C__%ad", "--date=short", "--shortstat", sha)
        date = ""
        for line in out.splitlines():
            if line.startswith("__C__"):
                date = line[5:].strip()
            elif "files changed" in line:
                match = _RE_FILES_CHANGED.search(line)
                if match and int(match.group(1)) > BULK_COMMIT_THRESHOLD:
                    bulk.add(sha)
        dates[sha] = date
    return None if sha in bulk else (dates[sha] or None)


def _signature_changes(root: Path, files: list[str], doc_commit: str) -> list[str]:
    # Graph range, not --since. A bare date fed to --since is completed with the
    # *current wall-clock time* by git's date parser, which made this boundary
    # drift with the time of day the harvest ran -- discovered when two fixture
    # tests failed before noon and passed after it. `doc_commit..HEAD` asks the
    # actual question ("what changed after the doc's last substantive edit")
    # with no date parsing at all.
    patch = _git(
        root, "log", "-p", "--no-color", "--unified=0", f"{doc_commit}..HEAD", "--", *files
    )
    lines = [line.rstrip() for line in patch.splitlines() if _RE_SIGNATURE.match(line)]
    return _drop_annotation_only(lines)


def _normalised_signature(line: str) -> str:
    """A signature stripped of sign, annotations and whitespace."""
    return re.sub(r"\s+", "", _RE_ANNOTATION.sub("", line[1:]))


def _drop_annotation_only(lines: list[str]) -> list[str]:
    """Cancel `-`/`+` pairs that differ only by annotations.

    Measured on this repo: **71% of raw signature changes were annotation-only**,
    and six of fourteen pages had no other kind at all. Their entire signal was the
    JSpecify nullability migration -- a repo-wide mechanical sweep that cannot make
    prose wrong. Left unfiltered it both invented drift and scrambled the ranking.

    Cancellation is by multiset so that a genuine change repeated across commits is
    not silently swallowed by one unrelated annotation edit.
    """
    added = [line for line in lines if line.startswith("+")]
    removed = [line for line in lines if line.startswith("-")]
    cancelled = Counter(_normalised_signature(l) for l in added) & Counter(
        _normalised_signature(l) for l in removed
    )

    budget = {"+": Counter(cancelled), "-": Counter(cancelled)}
    kept: list[str] = []
    for line in lines:
        counter = budget[line[0]]
        key = _normalised_signature(line)
        if counter.get(key, 0) > 0:
            counter[key] -= 1
            continue
        kept.append(line.strip())
    return kept


def _self_check(stats: HarvestStats) -> None:
    """A pre-filter that keeps nearly everything is not a pre-filter."""
    considered = stats.pages_with_enough_refs
    if considered < 20:
        return
    if stats.pages_with_signature_change == 0:
        raise HarvestDefect(
            f"{considered} pages reference framework types and none show a public "
            f"signature change. That is implausible for a repo this active; the "
            f"history or signature filter is likely broken. Refusing to emit."
        )
    ratio = stats.pages_with_signature_change / considered
    if ratio > 0.5:
        raise HarvestDefect(
            f"{stats.pages_with_signature_change}/{considered} pages ({ratio:.0%}) "
            f"flagged as drifted. The whole point is a narrow reading list, so a "
            f"filter -- most likely bulk-commit exclusion -- is not working. "
            f"Refusing to emit."
        )
