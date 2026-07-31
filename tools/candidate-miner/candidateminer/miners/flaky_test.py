"""Recurring test failures on green main: the flaky-test miner.

Consumes the `ci_failures` corpus (a deterministic API snapshot) plus the
working tree; calls nothing remote itself. The claim it makes is deliberately
narrow and fully checkable: *this test failed on main in N distinct runs, in
otherwise-passing builds, and here are the logs and build scans.* Whether the
fix is a timeout, a port, or a rewrite is judgement -- and therefore not this
tool's job.

Precision gates, in order:

* only runs classified ``test``, with a small failure count (mass breakage is a
  bad commit, not a flake);
* the same test must fail in **two or more distinct runs** -- singletons stay
  in the corpus, visible via ``corpus show``, and are never emitted;
* the test class must resolve to exactly one file in the working tree. A test
  that was deleted or renamed since, or a name that matches several files,
  cannot be traced to a line and therefore does not exist as a candidate.

Stabilising a flaky test is unusually junior-safe for this repo: maintainers
themselves demand robust tests (review corpus, PR #1612: a port-dependent test
"is guaranteed to fail... please make the test robust"), so the usual
presumed-deliberate hazard does not apply.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from ..contract import Candidate, CostClass, Locus, Score
from ..scoring import flaky_test_inputs, total
from .base import MinerContext, MinerDefect

NAME = "flaky_test"
VERSION = "1.0.0"
CATEGORY = "ci.flaky_test"

# A run in which more tests than this failed is breakage, not flakiness.
MAX_FAILED_PER_RUN = 10
MIN_OCCURRENCES = 2

# More distinct flaky tests than this in a ~90-day window on a repo whose main
# is green >90% of the time means the filters broke, not that CI melted.
SANITY_CEILING = 12

_TEST_ROOT_GLOBS = (
    "spring-*/src/test/java/**/{name}.java",
    "spring-*/src/testFixtures/java/**/{name}.java",
    "integration-tests/src/test/java/**/{name}.java",
)


class FlakyTestMiner:
    name = NAME
    version = VERSION
    cost_class = CostClass.A_API  # data provenance: needs a ci_failures harvest
    description = "tests that failed repeatedly on otherwise-green main"

    def run(self, ctx: MinerContext) -> Iterable[Candidate]:
        corpus = _load_corpus(ctx)
        if not corpus:
            # The harvester never writes an empty corpus (it stops before the
            # write), so a zero-record file means something else truncated it.
            # Emitting nothing against it would read as "no flaky tests".
            raise MinerDefect(
                "the ci_failures corpus exists but contains zero records; a "
                "harvest never writes an empty corpus, so the file is damaged. "
                "Re-run: miner.py harvest --source ci_failures"
            )
        groups: dict[str, list[tuple[dict, dict]]] = defaultdict(list)

        for run in corpus:
            if run.get("classification") != "test" or not run.get("log_available"):
                continue
            failed = max(
                (s.get("failed", 0) for s in run.get("test_summaries", [])), default=0
            )
            if failed > MAX_FAILED_PER_RUN:
                continue
            for test in run.get("failing_tests", []):
                identity = ".".join(
                    [test["test_class"], *test.get("nested_classes", []), test["method"]]
                )
                groups[identity].append((run, test))

        candidates = []
        for identity in sorted(groups):
            occurrences = groups[identity]
            distinct_runs = {run["run_id"] for run, _ in occurrences}
            if len(distinct_runs) < MIN_OCCURRENCES:
                continue
            candidate = self._build(ctx, identity, occurrences)
            if candidate is not None:
                candidates.append(candidate)

        if len(candidates) > SANITY_CEILING:
            raise MinerDefect(
                f"{len(candidates)} distinct recurring-failure tests found; on a "
                f"repo whose main branch is green the vast majority of the time "
                f"this is implausible, and a broken filter emitting noise is far "
                f"more likely than a CI collapse. Refusing to emit."
            )
        return sorted(candidates, key=lambda c: (-c.score.value, c.fingerprint))

    def _build(
        self, ctx: MinerContext, identity: str, occurrences: list[tuple[dict, dict]]
    ) -> Candidate | None:
        test = occurrences[0][1]
        path = _resolve_test_file(ctx.root, test["test_class"])
        if path is None:
            return None

        by_run: dict[int, dict[str, Any]] = {}
        for run, hit in occurrences:
            by_run.setdefault(
                run["run_id"],
                {
                    "run_id": run["run_id"],
                    "date": run.get("run_started_at", ""),
                    "workflow": run.get("workflow", ""),
                    "failed_tasks": run.get("failed_tasks", []),
                    "run_url": run.get("html_url", ""),
                    "scan_url": run.get("scan_url", ""),
                    "failed_in_run": max(
                        (s.get("failed", 0) for s in run.get("test_summaries", [])),
                        default=0,
                    ),
                    "cause": hit.get("cause", ""),
                },
            )
        runs = sorted(by_run.values(), key=lambda r: r["run_id"])

        tasks = sorted({t for r in runs for t in r["failed_tasks"]})
        line = test.get("at_line", 0) if test.get("at_file") == f"{test['test_class']}.java" else 0
        max_failed = max(r["failed_in_run"] for r in runs)

        inputs = flaky_test_inputs(
            occurrences=len(runs),
            distinct_tasks=len(tasks),
            max_failed_in_one_run=max_failed,
            has_line_evidence=line > 0,
        )
        return Candidate(
            category=CATEGORY,
            cost_class=self.cost_class,
            miner_name=self.name,
            miner_version=self.version,
            locus=Locus(path=path, line=line or 1),
            identity=identity,
            evidence={
                "test_class": test["test_class"],
                "nested_classes": test.get("nested_classes", []),
                "method": test["method"],
                "cause": test.get("cause", ""),
                "at": f"{test.get('at_file', '')}:{test.get('at_line', 0)}",
                "occurrence_count": len(runs),
                "failed_tasks": tasks,
                "occurrences": runs,
            },
            score=Score(value=total(inputs), inputs=inputs),
            pr_grouping_key=path,
            repo_commit=ctx.commit,
            repo_branch=ctx.branch,
        )


def _load_corpus(ctx: MinerContext) -> list[dict]:
    out_dir = ctx.options.get("out_dir")
    if not out_dir:
        raise OSError(
            "flaky_test needs the ci_failures corpus; no out_dir in context.\n"
            "  run: miner.py harvest --source ci_failures"
        )
    path = Path(out_dir) / "ci_failures.corpus.jsonl"
    if not path.is_file():
        raise OSError(
            f"no corpus at {path}\n  run: miner.py harvest --source ci_failures"
        )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _resolve_test_file(root: Path, class_name: str) -> str | None:
    """The class's single home in the tree, or None if absent/ambiguous."""
    matches: list[str] = []
    for pattern in _TEST_ROOT_GLOBS:
        matches.extend(
            p.relative_to(root).as_posix()
            for p in root.glob(pattern.format(name=class_name))
        )
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


MINER = FlakyTestMiner()
