#!/usr/bin/env python3
"""candidate-triage -- adjudicate a harvested corpus into ledger candidates.

The second entry point. `miner.py` is deterministic and never calls a model;
this one does, and is kept separate so that boundary is obvious from the
command you run. Both share the `candidateminer` package, the contract, and
the ledger, so an adjudicated candidate is declined exactly like a mined one.

    ./triage.py packet --source docs_drift            # assemble evidence, no model
    ./triage.py packet --source docs_drift --show 1   # read one packet in full
    ./triage.py run --source docs_drift --dry-run     # full pathway, stub adjudicator
    ./triage.py run --source docs_drift               # live: needs `pip install anthropic`

The model's job is narrow by construction: it may only cite a symbol it was
shown and pick a claim type from a fixed list, and every verdict is validated
against the evidence before it can reach the ledger. It cannot author a
candidate's identity -- that would make fingerprints unstable and silently
break the permanence of a human decline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from candidateminer import __version__, adjudication, gitutil, harvesters  # noqa: E402
from candidateminer.adjudication import (  # noqa: E402
    Packet,
    VerdictError,
    VerdictKind,
    build_packet,
    candidate_from_verdict,
    validate_verdict,
)
from candidateminer.adjudicators import (  # noqa: E402
    AdjudicatorError,
    ClaudeAdjudicator,
    StubAdjudicator,
    render_packet,
)
from candidateminer.contract import validate_record  # noqa: E402
from candidateminer.index import TypeIndex  # noqa: E402
from candidateminer.ledger import Ledger  # noqa: E402

DEFAULT_REPO_ROOT = _HERE.parent.parent
DEFAULT_LEDGER_DIR = _HERE / "ledger"
DEFAULT_OUT_DIR = _HERE / "out"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEFECT = 2

_LEDGER_NAME = "adjudicated_docs_drift"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (AdjudicatorError, VerdictError) as exc:
        print(f"\nDEFECT: {exc}\n", file=sys.stderr)
        return EXIT_DEFECT
    except (KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


def _load_packets(args: argparse.Namespace) -> list[Packet]:
    harvesters.get(args.source)  # unknown corpus is a distinct error
    corpus = args.out_dir / f"{args.source}.corpus.jsonl"
    if not corpus.is_file():
        raise OSError(
            f"no corpus at {corpus}\n"
            f"  run: miner.py harvest --source {args.source}"
        )
    records = [json.loads(l) for l in corpus.read_text(encoding="utf-8").splitlines() if l.strip()]
    root: Path = args.repo.resolve()
    index = TypeIndex.build(root)
    packets = [build_packet(root, record, index) for record in records]
    return packets[: args.limit] if args.limit else packets


def cmd_packet(args: argparse.Namespace) -> int:
    packets = _load_packets(args)
    if args.show is not None:
        if not 1 <= args.show <= len(packets):
            print(f"error: --show must be 1..{len(packets)}", file=sys.stderr)
            return EXIT_ERROR
        print(render_packet(packets[args.show - 1]))
        return EXIT_OK

    print(f"{len(packets)} packet(s) from {args.source}\n")
    for position, packet in enumerate(packets, 1):
        members = sum(len(t.members) for t in packet.types)
        print(
            f"  {position:>2}. {packet.page}\n"
            f"      {len(packet.sections)} section(s), {len(packet.types)} type(s), "
            f"{members} member(s), {len(packet.signature_changes)} change(s)\n"
            f"      {packet.evidence_digest[:26]}..."
        )
    print("\nInspect one in full with --show N. No model was called.")
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    packets = _load_packets(args)
    root: Path = args.repo.resolve()
    # `--dry-run` means "write nothing" (as in miner.py); `--adjudicator` chooses
    # who decides. Keeping them separate makes the write path exercisable without
    # spending money, and stops --dry-run from quietly meaning two things.
    use_stub = args.adjudicator == "stub" or (args.adjudicator is None and args.dry_run)
    adjudicator = StubAdjudicator() if use_stub else ClaudeAdjudicator(model=args.model)

    print(
        f"adjudicating {len(packets)} packet(s) from {args.source} "
        f"via {adjudicator.name}"
        + ("" if use_stub else f" ({args.model})")
        + ("  [DRY RUN - nothing will be written]" if args.dry_run else "")
    )
    print("-" * 66)

    accepted, rejected, discarded = [], 0, []
    for packet in packets:
        verdict = adjudicator.adjudicate(packet)
        if verdict.kind is not VerdictKind.ACCEPT:
            rejected += 1
            print(f"  reject   {packet.page}")
            continue
        try:
            validate_verdict(verdict, packet, root)
        except VerdictError as exc:
            # A claim the evidence does not support is dropped here, loudly,
            # rather than becoming a confident-looking ledger entry.
            discarded.append((packet.page, str(exc)))
            print(f"  DISCARD  {packet.page}\n             {exc}")
            continue
        accepted.append((packet, verdict))
        print(f"  ACCEPT   {packet.page}  [{verdict.claim_type.value}] {verdict.symbol}")

    print("-" * 66)
    print(f"  {len(accepted)} accepted, {rejected} rejected, {len(discarded)} discarded")

    if not accepted:
        print("\nNothing to add to the ledger.")
        return EXIT_OK

    commit = gitutil.head_commit(root)
    candidates = [
        candidate_from_verdict(p, v, commit=commit, branch=gitutil.current_branch(root))
        for p, v in accepted
    ]
    for candidate in candidates:
        validate_record(candidate.to_record())

    print()
    for candidate in sorted(candidates, key=lambda c: -c.score.value):
        evidence = candidate.evidence
        print(f"[{candidate.score.value:>3}] {evidence['symbol']} ({evidence['claim_type']})")
        print(f"      {candidate.locus.path}:{candidate.locus.line}")
        print(f"      {evidence['triage']['rationale'][:100]}")
        print(f"      {candidate.fingerprint}")

    if args.dry_run:
        print("\nDry run: ledger untouched.")
        return EXIT_OK

    ledger_path = args.ledger_dir / f"{_LEDGER_NAME}.jsonl"
    ledger = Ledger.load(ledger_path)
    result = ledger.merge(candidates, commit=commit, root=root)
    changed = ledger.save(ledger_path)

    out_path = args.out_dir / f"{_LEDGER_NAME}.candidates.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "".join(c.to_jsonl() + "\n" for c in result.emitted), encoding="utf-8"
    )

    print(
        f"\n{len(result.emitted)} emitted ({len(result.newly_seen)} new, "
        f"{len(result.suppressed)} suppressed by a previous decline)"
    )
    print(f"ledger -> {ledger_path}  ({'changed' if changed else 'unchanged'})")
    print(f"Decline one with: miner.py ledger decline <fingerprint> --reason '...'")
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triage.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"candidate-triage {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--source", default="docs_drift", help="corpus name")
        sub.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT)
        sub.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
        sub.add_argument("--limit", type=int, default=0, help="packets to process; 0 = all")

    p_packet = subparsers.add_parser("packet", help="assemble evidence packets (no model)")
    common(p_packet)
    p_packet.add_argument("--show", type=int, help="print packet N in full")
    p_packet.set_defaults(handler=cmd_packet)

    p_run = subparsers.add_parser("run", help="adjudicate packets into ledger candidates")
    common(p_run)
    p_run.add_argument("--ledger-dir", type=Path, default=DEFAULT_LEDGER_DIR)
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the full pathway with a stub adjudicator; calls no model",
    )
    p_run.add_argument("--model", default="claude-opus-5")
    p_run.add_argument(
        "--adjudicator",
        choices=("claude", "stub"),
        help="who decides; defaults to stub under --dry-run, claude otherwise",
    )
    p_run.set_defaults(handler=cmd_run)

    return parser


if __name__ == "__main__":
    raise SystemExit(main())
