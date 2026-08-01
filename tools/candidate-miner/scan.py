#!/usr/bin/env python3
"""Workflow support: compute what the scan workflow's agent may propose.

This is the deterministic half of the `candidate-scan` workflow. It runs the
miners through the same CLI surface the tests cover, drops every candidate
that already has a GitHub issue -- open *or* closed, so closing an issue is a
veto the scan respects -- and writes the ranked, capped remainder to
``out/proposable.json``. The agent downstream may only file what this file
contains.

Deduplication key: the fingerprint marker the agent embeds in every issue body
(``candidate-miner:fingerprint sha256:...`` as a visible inline-code line --
NOT an HTML comment, which gh-aw's safe-output sanitizer strips from
agent-authored content; the regex tolerates the comment-wrapped form too).
The issue listing is fetched **uncached**, unlike harvest traffic: a cached
listing would miss issues filed after the cache was written and re-propose
their candidates.

Still no LLM here. Exit codes match miner.py: 0 success, 1 error, 2 defect.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import miner  # noqa: E402
from candidateminer.github import GitHubClient, GitHubError, discover_token  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEFECT = 2

ISSUE_LABEL = "candidate-miner"
MARKER_RE = re.compile(r"candidate-miner:fingerprint\s+(sha256:[0-9a-f]{64})\b")

# Corpus-consuming miners run only when their harvest succeeded this run; the
# others mine the checkout directly and always run.
CHECKOUT_MINERS = ("docs_dead_ref", "config_dead_entry")
CORPUS_MINERS = {"flaky_test": "ci_failures"}
HARVEST_LIMIT = 100


class ScanError(RuntimeError):
    def __init__(self, message: str, exit_code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def run_miner_cli(argv: list[str]) -> tuple[int, str, str]:
    """Invoke miner.py in-process; same CLI contract, no subprocess."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = miner.main(argv)
    return code, out.getvalue(), err.getvalue()


def parse_candidates(jsonl: str) -> list[dict]:
    records = []
    for line in jsonl.splitlines():
        line = line.strip()
        if line.startswith("{"):
            records.append(json.loads(line))
    return records


def mine(runner, passthrough: list[str]) -> tuple[list[dict], list[str]]:
    """Run harvest + miners; return (candidates, warnings).

    A failed harvest skips the miners that consume it, with a warning -- the
    network is an environmental hazard, not a tool defect, and losing this
    week's flaky-test proposals must not cost the docs proposals too. A miner
    that itself fails is a different matter and stops the scan loudly, keeping
    exit code 2 for defects.
    """
    warnings: list[str] = []
    names = list(CHECKOUT_MINERS)

    for name, corpus in CORPUS_MINERS.items():
        code, _, err = runner(
            ["harvest", "--source", corpus, "--limit", str(HARVEST_LIMIT)]
            + _harvest_passthrough(passthrough)
        )
        if code == EXIT_OK:
            names.append(name)
        else:
            warnings.append(
                f"harvest of {corpus} failed (exit {code}); miner {name} "
                f"skipped this run: {err.strip().splitlines()[-1] if err.strip() else 'no detail'}"
            )

    candidates: list[dict] = []
    for name in names:
        code, out, err = runner(
            ["run", "--miner", name, "--dry-run", "--format", "jsonl"] + passthrough
        )
        if code != EXIT_OK:
            raise ScanError(
                f"miner {name} failed (exit {code}):\n{err.strip()}", exit_code=code
            )
        candidates.extend(parse_candidates(out))
    return candidates, warnings


def _harvest_passthrough(passthrough: list[str]) -> list[str]:
    # harvest shares --out-dir with run but has no --ledger-dir or --repo path.
    kept, skip_next = [], False
    for arg in passthrough:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--ledger-dir", "--repo"):
            skip_next = True
            continue
        kept.append(arg)
    return kept


def filed_fingerprints(client: GitHubClient, repo_slug: str) -> set[str]:
    """Fingerprints of every candidate that already has an issue, any state."""
    path = f"/repos/{repo_slug}/issues?labels={ISSUE_LABEL}&state=all&per_page=100"
    filed: set[str] = set()
    for item in client.paginate(path):
        if "pull_request" in item:
            continue
        filed.update(MARKER_RE.findall(item.get("body") or ""))
    if client.truncated:
        raise ScanError(
            "issue listing pagination was truncated; a partial view could "
            "re-propose an already-filed candidate. Aborting."
        )
    return filed


def select(candidates: list[dict], filed: set[str], cap: int) -> tuple[list[dict], int]:
    """Rank unfiled candidates by score (fingerprint breaks ties) and cap."""
    unfiled = [c for c in candidates if c["fingerprint"] not in filed]
    unfiled.sort(key=lambda c: (-c["score"]["value"], c["fingerprint"]))
    return unfiled[:cap], len(unfiled)


def write_outputs(
    out_dir: Path,
    selected: list[dict],
    *,
    counts: dict,
    warnings: list[str],
    cap: int,
) -> Path:
    payload = {
        "schema_version": 1,
        "max": cap,
        "counts": counts,
        "warnings": warnings,
        "candidates": selected,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "proposable.json"
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
            fh.write(f"count={len(selected)}\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(_summary_markdown(selected, counts, warnings))
    return path


def _summary_markdown(selected: list[dict], counts: dict, warnings: list[str]) -> str:
    lines = ["## candidate-scan", ""]
    lines.append(
        f"mined {counts['mined']}, already filed {counts['already_filed']}, "
        f"proposable {counts['proposable']}, selected {counts['selected']}"
    )
    if selected:
        lines += ["", "| score | category | identity | where |", "|--:|---|---|---|"]
        for c in selected:
            lines.append(
                f"| {c['score']['value']} | {c['category']} | `{c['identity']}` "
                f"| `{c['locus']['path']}:{c['locus']['line']}` |"
            )
    for warning in warnings:
        lines += ["", f"> **warning:** {warning}"]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None, *, runner=None, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scan.py", description=__doc__)
    parser.add_argument(
        "--repo-slug",
        required=True,
        help="owner/name whose issues dedupe the proposals (the fork, in CI)",
    )
    parser.add_argument("--max", type=int, default=3, help="batch cap (default: %(default)s)")
    parser.add_argument("--repo", type=Path, help="repo root to scan (passed to miners)")
    parser.add_argument("--ledger-dir", type=Path, help="passed to miners")
    parser.add_argument(
        "--out-dir", type=Path, default=miner.DEFAULT_OUT_DIR, help="passed to miners"
    )
    args = parser.parse_args(argv)

    passthrough = ["--out-dir", str(args.out_dir)]
    if args.repo:
        passthrough += ["--repo", str(args.repo)]
    if args.ledger_dir:
        passthrough += ["--ledger-dir", str(args.ledger_dir)]

    runner = runner or run_miner_cli
    if client is None:
        client = GitHubClient(token=discover_token(), cache_dir=None)

    try:
        candidates, warnings = mine(runner, passthrough)
        filed = filed_fingerprints(client, args.repo_slug)
        selected, proposable = select(candidates, filed, args.max)
    except ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    counts = {
        "mined": len(candidates),
        "already_filed": len(candidates) - proposable,
        "proposable": proposable,
        "selected": len(selected),
    }
    path = write_outputs(
        args.out_dir, selected, counts=counts, warnings=warnings, cap=args.max
    )

    print(f"proposable -> {path}")
    print(
        f"  mined {counts['mined']}, already filed {counts['already_filed']}, "
        f"proposable {counts['proposable']}, selected {counts['selected']} (cap {args.max})"
    )
    for warning in warnings:
        print(f"  warning: {warning}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
