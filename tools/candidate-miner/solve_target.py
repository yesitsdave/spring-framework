#!/usr/bin/env python3
"""Workflow support: verify a filed candidate before and after solving it.

The deterministic brackets around the `candidate-solve` workflow's agent.
Given an issue number, this resolves the issue's fingerprint marker to a
ledger entry, re-runs the one miner that owns it, and reports a verdict:

  confirmed          the candidate still exists at HEAD (safe to solve);
                     the full candidate record is included for the agent
  gone               the miner no longer emits it -- run *after* the fix,
                     this is the proof the fix worked; run *before*, it
                     means the issue is stale and should be closed
  no_marker          the issue body carries no fingerprint marker
  ambiguous_marker   more than one distinct fingerprint in one issue
  unknown_fingerprint  not present in any committed ledger
  declined           a human vetoed this permanently; do not solve it
  unsupported        owned by a corpus miner (flaky_test) or an adjudicated
                     ledger -- evidence is historical, so "gone" cannot be
                     proven mechanically; a human decides

Verdicts are data, not errors: every verdict exits 0 and writes
``out/solve_target.json``. Exit 1 is an infrastructure failure (network,
missing issue), exit 2 a miner defect. Still no LLM here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

import miner  # noqa: E402
import scan  # noqa: E402
from candidateminer import miners  # noqa: E402
from candidateminer.github import GitHubClient, GitHubError, discover_token  # noqa: E402
from candidateminer.ledger import Ledger  # noqa: E402
from candidateminer.contract import State  # noqa: E402

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEFECT = 2


def fingerprint_from_issue(client: GitHubClient, repo_slug: str, issue: int) -> tuple[str | None, str]:
    """Return (fingerprint, verdict): verdict is empty when exactly one found."""
    payload = client.get(f"/repos/{repo_slug}/issues/{issue}")
    found = sorted(set(scan.MARKER_RE.findall(payload.get("body") or "")))
    if not found:
        return None, "no_marker"
    if len(found) > 1:
        return None, "ambiguous_marker"
    return found[0], ""


def locate_in_ledgers(ledger_dir: Path, fingerprint: str) -> tuple[str, object] | None:
    """(ledger stem, entry) for the fingerprint, or None. Stems are unique."""
    for path in sorted(ledger_dir.glob("*.jsonl")):
        entry = Ledger.load(path).get(fingerprint)
        if entry is not None:
            return path.stem, entry
    return None


def verify(fingerprint: str, miner_name: str, runner, passthrough: list[str]) -> tuple[str, dict | None]:
    """Run the owning miner; (verdict, record-if-confirmed)."""
    code, out, err = runner(
        ["run", "--miner", miner_name, "--dry-run", "--format", "jsonl"] + passthrough
    )
    if code != EXIT_OK:
        raise scan.ScanError(
            f"miner {miner_name} failed (exit {code}):\n{err.strip()}", exit_code=code
        )
    for record in scan.parse_candidates(out):
        if record["fingerprint"] == fingerprint:
            return "confirmed", record
    return "gone", None


def main(argv: list[str] | None = None, *, runner=None, client: GitHubClient | None = None) -> int:
    parser = argparse.ArgumentParser(prog="solve_target.py", description=__doc__)
    parser.add_argument("--repo-slug", help="owner/name holding the issue (with --issue)")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--issue", type=int, help="issue number to verify (needs --repo-slug)")
    source.add_argument(
        "--fingerprint",
        help="verify a fingerprint directly, no issue fetch -- the offline "
        "post-fix check: expect verdict `gone` after a correct fix",
    )
    parser.add_argument("--repo", type=Path, help="repo root to scan (passed to miners)")
    parser.add_argument(
        "--ledger-dir", type=Path, default=miner.DEFAULT_LEDGER_DIR, help="committed ledgers"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=miner.DEFAULT_OUT_DIR, help="where the verdict is written"
    )
    args = parser.parse_args(argv)

    passthrough = ["--out-dir", str(args.out_dir), "--ledger-dir", str(args.ledger_dir)]
    if args.repo:
        passthrough += ["--repo", str(args.repo)]

    if args.issue is not None and not args.repo_slug:
        parser.error("--issue requires --repo-slug")

    runner = runner or scan.run_miner_cli

    result: dict = {"schema_version": 1, "issue": args.issue}
    try:
        if args.fingerprint is not None:
            fingerprint, verdict = args.fingerprint, ""
        else:
            if client is None:
                # Uncached for the same reason as scan.py: the issue body must
                # be current, not a snapshot from before the marker landed.
                client = GitHubClient(token=discover_token(), cache_dir=None)
            fingerprint, verdict = fingerprint_from_issue(client, args.repo_slug, args.issue)
        result["fingerprint"] = fingerprint
        if not verdict:
            located = locate_in_ledgers(args.ledger_dir, fingerprint)
            if located is None:
                verdict = "unknown_fingerprint"
            else:
                stem, entry = located
                result["ledger"] = stem
                result["identity"] = entry.identity
                if entry.state is State.DECLINED:
                    verdict = "declined"
                    result["reason"] = entry.reason
                elif stem not in miners.REGISTRY or stem in scan.CORPUS_MINERS:
                    verdict = "unsupported"
                else:
                    result["miner"] = stem
                    verdict, record = verify(fingerprint, stem, runner, passthrough)
                    if record is not None:
                        result["candidate"] = record
    except scan.ScanError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except GitHubError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    result["verdict"] = verdict
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / "solve_target.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"verdict -> {path}")
    print(f"  issue #{args.issue}: {verdict}"
          + (f"  ({result.get('identity', '')})" if result.get("identity") else ""))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
