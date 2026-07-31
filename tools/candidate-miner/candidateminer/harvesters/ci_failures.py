"""Harvest failed CI runs on main, classified, with failing tests extracted.

Why this corpus. The build applies the Gradle test-retry plugin with
``failOnPassedAfterRetry = true`` (buildSrc TestConventions), so on CI a flaky
test fails the build *even when a retry passes*. A failed run on main whose next
runs are green is therefore a high-precision flake signal -- and main-branch
workflows only, deliberately: a failure on a PR branch is usually caused by the
PR, which makes attribution worthless.

Recon (2026-07-31) showed the job console log names failing tests precisely
(``Class > nested > method() FAILED`` plus ``Cause at File.java:NNN``), so
symbol-level extraction is possible without artifacts, which this repo's CI does
not upload. The known limits, recorded honestly per record rather than hidden:

* Actions retains logs ~90 days; older failures appear with ``log_available:
  false`` and nothing more.
* Non-test failures (javadoc, antora, nohttp) are kept and classified rather
  than dropped -- they are CI-quality evidence in their own right (recon found
  javadoc breakage reaching main precisely because the aggregate javadoc task
  does not run on PR builds).

Deterministic stage: no judgement here, no model. The `flaky_test` miner
consumes this corpus downstream.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..contract import CostClass
from ..github import GitHubClient
from .base import HarvestDefect

CORPUS = "ci_failures"
COST_CLASS = CostClass.A_API
DESCRIPTION = "failed main-branch CI runs, classified, failing tests extracted"
SCHEMA_VERSION = 1

# Main-branch workflows whose failures are attributable to the tree rather than
# to an unmerged PR. Identified during recon; referenced by file name, which the
# Actions API accepts directly.
WORKFLOWS = ("ci.yml", "build-and-deploy-snapshot.yml")

_RE_TS_PREFIX = re.compile(r"^\S+Z ")
_RE_TASK_FAILED = re.compile(r"> Task (:\S+) FAILED")
# `ClassName > [Nested >] method() FAILED` -- segments joined by " > ". The
# first segment must look like a Java type name so ordinary prose lines ending
# in FAILED (and "> Task ..." lines) cannot match.
_RE_TEST_FAILED = re.compile(r"^([A-Z][\w$]*(?: > [^>]+?)+) FAILED$")
_RE_CAUSE = re.compile(r"^\s+([\w.$]+(?:Error|Exception)[\w.$]*)(?: at ([\w$]+\.java):(\d+))?")
_RE_TEST_SUMMARY = re.compile(r"(\d+) tests completed, (\d+) failed(?:, (\d+) skipped)?")
_RE_SCAN_URL = re.compile(r"https://ge\.spring\.io/s/[a-z0-9]+")
_RE_JDK_TEST_TASK = re.compile(r"java\d+Test$")


@dataclass(frozen=True)
class FailedRun:
    run_id: int
    workflow: str
    run_started_at: str
    head_sha: str
    html_url: str
    failed_jobs: tuple[str, ...]
    log_available: bool
    failed_tasks: tuple[str, ...]
    test_summaries: tuple[dict[str, int], ...]
    failing_tests: tuple[dict[str, Any], ...]
    scan_url: str
    classification: str

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "corpus": CORPUS,
            "run_id": self.run_id,
            "workflow": self.workflow,
            "run_started_at": self.run_started_at,
            "head_sha": self.head_sha,
            "html_url": self.html_url,
            "failed_jobs": list(self.failed_jobs),
            "log_available": self.log_available,
            "failed_tasks": list(self.failed_tasks),
            "test_summaries": list(self.test_summaries),
            "failing_tests": list(self.failing_tests),
            "scan_url": self.scan_url,
            "classification": self.classification,
        }


@dataclass
class HarvestStats:
    runs_listed: int = 0
    failures_seen: int = 0
    logs_fetched: int = 0
    logs_unavailable: int = 0
    failing_tests_extracted: int = 0
    window_oldest: str = ""
    window_newest: str = ""
    by_workflow: dict[str, dict[str, int]] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "runs_listed": self.runs_listed,
            "failures_seen": self.failures_seen,
            "logs_fetched": self.logs_fetched,
            "logs_unavailable": self.logs_unavailable,
            "failing_tests_extracted": self.failing_tests_extracted,
            "window_oldest": self.window_oldest,
            "window_newest": self.window_newest,
        }
        for workflow, counts in sorted(self.by_workflow.items()):
            out[f"{workflow} runs/failures"] = f"{counts['runs']}/{counts['failures']}"
        return out


def harvest(
    client: GitHubClient, repo: str, *, limit: int = 100
) -> tuple[list[FailedRun], HarvestStats]:
    """`limit` is completed main-branch runs examined *per workflow*."""
    stats = HarvestStats()
    kept: list[FailedRun] = []
    dates: list[str] = []

    for workflow in WORKFLOWS:
        counts = stats.by_workflow.setdefault(workflow, {"runs": 0, "failures": 0})
        path = (
            f"/repos/{repo}/actions/workflows/{workflow}/runs"
            f"?branch=main&status=completed&per_page=100"
        )
        seen = 0
        for run in client.paginate(path):
            runs = run.get("workflow_runs") if isinstance(run, dict) else None
            for entry in runs if runs is not None else [run]:
                if not isinstance(entry, dict) or "id" not in entry:
                    continue
                if seen >= limit:
                    break
                seen += 1
                stats.runs_listed += 1
                counts["runs"] += 1
                if entry.get("run_started_at"):
                    dates.append(entry["run_started_at"])
                if entry.get("conclusion") != "failure":
                    continue
                stats.failures_seen += 1
                counts["failures"] += 1
                kept.append(_harvest_run(client, repo, workflow, entry, stats))
            if seen >= limit:
                break

    stats.window_oldest = min(dates) if dates else ""
    stats.window_newest = max(dates) if dates else ""
    _self_check(stats, kept)
    return sorted(kept, key=lambda r: (r.workflow, r.run_id)), stats


def _harvest_run(
    client: GitHubClient, repo: str, workflow: str, run: dict, stats: HarvestStats
) -> FailedRun:
    # Paginated: a matrix run can carry more than one 30-item default page of
    # jobs, and a sole failed job beyond page one would silently read as
    # "no failed jobs, log expired" rather than as a parse target.
    jobs: list[dict] = []
    for page in client.paginate(f"/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"):
        if isinstance(page, dict):
            jobs.extend(page.get("jobs", []))
    failed_jobs = [j for j in jobs if j.get("conclusion") == "failure"]

    text = ""
    log_available = False
    for job in failed_jobs:
        body = client.get_text(f"/repos/{repo}/actions/jobs/{job['id']}/logs")
        if body is not None:
            log_available = True
            text += body + "\n"
    if log_available:
        stats.logs_fetched += 1
    else:
        stats.logs_unavailable += 1

    parsed = parse_log(text) if log_available else _EMPTY_PARSE
    stats.failing_tests_extracted += len(parsed["failing_tests"])

    return FailedRun(
        run_id=run["id"],
        workflow=workflow,
        run_started_at=run.get("run_started_at") or "",
        head_sha=run.get("head_sha") or "",
        html_url=run.get("html_url") or "",
        failed_jobs=tuple(j.get("name") or "" for j in failed_jobs),
        log_available=log_available,
        failed_tasks=tuple(parsed["failed_tasks"]),
        test_summaries=tuple(parsed["test_summaries"]),
        failing_tests=tuple(parsed["failing_tests"]),
        scan_url=parsed["scan_url"],
        classification=_classify(parsed["failed_tasks"], parsed["failing_tests"]),
    )


_EMPTY_PARSE: dict[str, Any] = {
    "failed_tasks": (),
    "test_summaries": (),
    "failing_tests": (),
    "scan_url": "",
}


def parse_log(text: str) -> dict[str, Any]:
    """Extract structure from a job console log. Pure function, order-stable."""
    lines = [_RE_TS_PREFIX.sub("", line) for line in text.splitlines()]

    failed_tasks: list[str] = []
    failing_tests: list[dict[str, Any]] = []
    summaries: list[dict[str, int]] = []
    scan_url = ""

    for position, line in enumerate(lines):
        task = _RE_TASK_FAILED.search(line)
        if task and task.group(1) not in failed_tasks:
            failed_tasks.append(task.group(1))
            continue

        summary = _RE_TEST_SUMMARY.search(line)
        if summary:
            summaries.append(
                {
                    "completed": int(summary.group(1)),
                    "failed": int(summary.group(2)),
                    "skipped": int(summary.group(3) or 0),
                }
            )
            continue

        scan = _RE_SCAN_URL.search(line)
        if scan:
            scan_url = scan.group(0)
            continue

        test = _RE_TEST_FAILED.match(line)
        if test:
            entry = _parse_test_failure(test.group(1), lines, position)
            if entry is not None and entry not in failing_tests:
                failing_tests.append(entry)

    return {
        "failed_tasks": failed_tasks,
        "test_summaries": summaries,
        "failing_tests": failing_tests,
        "scan_url": scan_url,
    }


def _parse_test_failure(
    display: str, lines: list[str], position: int
) -> dict[str, Any] | None:
    """`Class > [Nested >] method(args) [> params] FAILED` -> structured entry.

    The method is the first segment after the class that carries parentheses;
    intermediate paren-free segments are nested class display names. Anything
    after the method segment is JUnit parameterisation detail -- real for
    display, but excluded from identity so `[1]`/`[2]` variants of one test
    collapse together.
    """
    segments = [s.strip() for s in display.split(" > ")]
    if len(segments) < 2:
        return None
    cls = segments[0]
    nested: list[str] = []
    method = ""
    for segment in segments[1:]:
        if "(" in segment:
            method = segment.split("(", 1)[0]
            break
        nested.append(segment)
    if not method:
        return None

    cause, at_file, at_line = "", "", 0
    if position + 1 < len(lines):
        matched = _RE_CAUSE.match(lines[position + 1])
        if matched:
            cause = matched.group(1)
            at_file = matched.group(2) or ""
            at_line = int(matched.group(3) or 0)

    return {
        "test_class": cls,
        "nested_classes": nested,
        "method": method,
        "display": display[:200],
        "cause": cause,
        "at_file": at_file,
        "at_line": at_line,
    }


def _classify(failed_tasks: list[str], failing_tests: list[dict[str, Any]]) -> str:
    def leaf(task: str) -> str:
        return task.rsplit(":", 1)[-1]

    if failing_tests or any(
        leaf(t) == "test" or _RE_JDK_TEST_TASK.fullmatch(leaf(t)) for t in failed_tasks
    ):
        return "test"
    if any(leaf(t) == "javadoc" or "dokka" in leaf(t).lower() for t in failed_tasks):
        return "javadoc"
    if any(leaf(t) == "antora" for t in failed_tasks):
        return "antora"
    if any("nohttp" in t.lower() for t in failed_tasks):
        return "nohttp"
    return "other" if failed_tasks else "unknown"


def _self_check(stats: HarvestStats, kept: list[FailedRun]) -> None:
    """Refuse to emit a corpus the parser obviously failed to read."""
    parsed_any = any(r.failed_tasks for r in kept if r.log_available)
    if stats.logs_fetched >= 3 and not parsed_any:
        raise HarvestDefect(
            f"fetched {stats.logs_fetched} failed-job logs and parsed a failing "
            f"Gradle task out of none of them. Gradle always names the failed "
            f"task; the log parser (or the timestamp stripper) is broken. "
            f"Refusing to emit."
        )
    if stats.failures_seen > 0 and stats.logs_fetched + stats.logs_unavailable == 0:
        raise HarvestDefect(
            f"saw {stats.failures_seen} failed runs but attempted zero log "
            f"fetches -- the jobs lookup is broken. Refusing to emit."
        )
