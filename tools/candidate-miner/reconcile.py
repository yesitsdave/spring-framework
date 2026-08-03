#!/usr/bin/env python3
"""Workflow support: fold issue outcomes back into the committed ledger.

The GitHub issue is the human interface; the ledger is the machine memory.
This is the one-way sync from gestures to state, run by the
`candidate-ledger-reconcile` workflow whenever a candidate-miner issue
changes state (plus a weekly sweep for anything missed):

    issue open                    ->  queued    (from new)
    issue closed as completed     ->  done
    issue closed as not planned   ->  declined  (permanent, with provenance)
    reopened after done           ->  queued    (work resumed)
    declined vs. anything else    ->  reported, never changed -- a ledger
                                      decline is terminal by design and only
                                      a deliberate hand edit may undo it

This never runs a miner and never calls a model. It reads issues, rewrites
JSONL, and reports; committing the result is the workflow's business.
Exit codes: 0 success (whether or not anything changed), 1 error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import miner  # noqa: E402
import scan  # noqa: E402
from candidateminer.contract import State  # noqa: E402
from candidateminer.github import GitHubClient, GitHubError, discover_token  # noqa: E402
from candidateminer.ledger import Ledger  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1


@dataclass
class Report:
    transitions: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    unknown: list[dict] = field(default_factory=list)
    ambiguous: list[int] = field(default_factory=list)
    changed_ledgers: list[str] = field(default_factory=list)


def _target_state(issue: dict) -> State:
    if issue.get("state") == "open":
        return State.QUEUED
    if issue.get("state_reason") == "not_planned":
        return State.DECLINED
    return State.DONE


def _closed_by(client: GitHubClient, repo_slug: str, number: int) -> str:
    payload = client.get(f"/repos/{repo_slug}/issues/{number}")
    closed_by = payload.get("closed_by") or {}
    return closed_by.get("login") or "unknown"


def reconcile(client: GitHubClient, repo_slug: str, ledger_dir: Path) -> Report:
    ledgers = {
        path.stem: (path, Ledger.load(path)) for path in sorted(ledger_dir.glob("*.jsonl"))
    }
    by_fingerprint = {
        entry.fingerprint: stem
        for stem, (_, ledger) in ledgers.items()
        for entry in ledger.sorted_entries()
    }

    report = Report()
    path = f"/repos/{repo_slug}/issues?labels={scan.ISSUE_LABEL}&state=all&per_page=100"
    for issue in client.paginate(path):
        if "pull_request" in issue:
            continue  # PR bodies quote fingerprints too; issue state drives the ledger
        number = issue["number"]
        found = sorted(set(scan.MARKER_RE.findall(issue.get("body") or "")))
        if len(found) > 1:
            report.ambiguous.append(number)
            continue
        for fingerprint in found:
            stem = by_fingerprint.get(fingerprint)
            if stem is None:
                report.unknown.append({"issue": number, "fingerprint": fingerprint})
                continue
            _, ledger = ledgers[stem]
            entry = ledger.get(fingerprint)
            target = _target_state(issue)
            if entry.state is target:
                continue
            if entry.state is State.DECLINED:
                report.conflicts.append({
                    "issue": number, "fingerprint": fingerprint, "ledger": stem,
                    "held": "declined is terminal; edit the ledger by hand if truly intended",
                })
                continue
            kwargs = {}
            if target is State.DECLINED:
                kwargs = {
                    "reason": f"issue #{number} closed as not planned",
                    "decided_by": _closed_by(client, repo_slug, number),
                }
            ledger.set_state(fingerprint, target, **kwargs)
            report.transitions.append({
                "issue": number, "fingerprint": fingerprint, "ledger": stem,
                "from": entry.state.value, "to": target.value,
            })

    if client.truncated:
        raise GitHubError(
            "issue listing pagination was truncated; reconciling from a partial "
            "view could miss a veto. Aborting without writing."
        )

    for stem, (path_, ledger) in ledgers.items():
        if ledger.save(path_):
            report.changed_ledgers.append(stem)
    return report


def _summary(report: Report) -> str:
    lines = ["## ledger-reconcile", ""]
    lines.append(
        f"transitions {len(report.transitions)}, conflicts {len(report.conflicts)}, "
        f"unknown fingerprints {len(report.unknown)}, ambiguous issues {len(report.ambiguous)}"
    )
    for t in report.transitions:
        lines.append(f"- #{t['issue']}: `{t['ledger']}` {t['from']} → {t['to']}")
    for c in report.conflicts:
        lines.append(f"- #{c['issue']}: **held** — {c['held']}")
    for u in report.unknown:
        lines.append(f"- #{u['issue']}: unknown fingerprint `{u['fingerprint'][:18]}…`")
    for n in report.ambiguous:
        lines.append(f"- #{n}: multiple distinct fingerprints; skipped")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reconcile.py", description=__doc__)
    parser.add_argument("--repo-slug", required=True, help="owner/name whose issues drive the ledger")
    parser.add_argument(
        "--ledger-dir", type=Path, default=miner.DEFAULT_LEDGER_DIR, help="committed ledgers"
    )
    args = parser.parse_args(argv)

    if client is None:
        # Uncached: reconciling from a stale issue snapshot could miss a veto.
        client = GitHubClient(token=discover_token(), cache_dir=None)

    try:
        report = reconcile(client, args.repo_slug, args.ledger_dir)
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    changed = bool(report.changed_ledgers)
    print(_summary(report))
    print(f"ledgers changed: {', '.join(report.changed_ledgers) or 'none'}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"changed={'true' if changed else 'false'}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(_summary(report))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
