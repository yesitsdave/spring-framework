#!/usr/bin/env python3
"""candidate-miner -- deterministic mining for junior-contributor candidates.

Standalone CLI. Not a Gradle plugin, absent from settings.gradle, and it never
modifies anything it scans. Python 3.11+, standard library only.

    ./miner.py list
    ./miner.py run --miner docs_dead_ref
    ./miner.py run --miner docs_dead_ref --dry-run
    ./miner.py ledger show --state new
    ./miner.py ledger decline sha256:... --reason "intentional historical note"

No LLM calls anywhere: same commit in, same bytes out. Agentic triage happens
downstream, against this tool's output.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from candidateminer import __version__, gitutil, harvesters, miners, report  # noqa: E402
from candidateminer.github import GitHubClient, GitHubError, discover_token  # noqa: E402
from candidateminer.harvesters.base import HarvestDefect  # noqa: E402
from candidateminer.contract import CostClass, State, canonical_json, validate_record  # noqa: E402
from candidateminer.ledger import Ledger, LedgerError  # noqa: E402
from candidateminer.miners import MinerContext, MinerDefect  # noqa: E402

DEFAULT_REPO_ROOT = _HERE.parent.parent
DEFAULT_LEDGER_DIR = _HERE / "ledger"
DEFAULT_OUT_DIR = _HERE / "out"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_DEFECT = 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (MinerDefect, HarvestDefect) as exc:
        print(f"\nDEFECT: {exc}\n", file=sys.stderr)
        return EXIT_DEFECT
    except (LedgerError, GitHubError, KeyError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


# ---------------------------------------------------------------- commands


def cmd_list(args: argparse.Namespace) -> int:
    print(f"candidate-miner {__version__}\n")
    for cost_class in CostClass:
        found = miners.by_cost_class(cost_class)
        print(f"  {cost_class.value}  ({_COST_CLASS_BLURB[cost_class]})")
        if not found:
            print("      (no miners yet)")
        for miner in found:
            print(f"      {miner.name:<20} v{miner.version}  {miner.description}")
        print()
    return EXIT_OK


def cmd_run(args: argparse.Namespace) -> int:
    root: Path = args.repo.resolve()
    if not root.is_dir():
        print(f"error: repo root does not exist: {root}", file=sys.stderr)
        return EXIT_ERROR

    miner = miners.get(args.miner)
    ctx = MinerContext(
        root=root,
        commit=gitutil.head_commit(root),
        branch=gitutil.current_branch(root),
        options={
            "include_experimental": args.include_experimental,
            "sanity_ceiling": args.sanity_ceiling,
            # Corpus-consuming miners (flaky_test) read harvested input from here.
            "out_dir": str(args.out_dir),
        },
    )

    candidates = list(miner.run(ctx))
    for candidate in candidates:
        validate_record(candidate.to_record())

    ledger_path = args.ledger_dir / f"{miner.name}.jsonl"
    ledger = Ledger.load(ledger_path)
    result = ledger.merge(candidates, commit=ctx.commit, root=root)

    if args.format in ("summary", "both"):
        print(
            report.render(
                miner_name=miner.name,
                result=result,
                ledger=ledger,
                commit=ctx.commit,
                branch=ctx.branch,
                index_size=len(ctx.type_index),
                dry_run=args.dry_run,
            )
        )

    payload = "".join(c.to_jsonl() + "\n" for c in result.emitted)

    if args.format in ("jsonl", "both"):
        sys.stdout.write(payload)

    if args.dry_run:
        if args.format in ("summary", "both"):
            print("Dry run: ledger and output files left untouched.")
        return EXIT_OK

    out_path = args.out_dir / f"{miner.name}.candidates.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_changed = _write_if_changed(out_path, payload)
    ledger_changed = ledger.save(ledger_path)

    # The run manifest is the only artefact carrying wall-clock time. Keeping it
    # out of the candidate records is what lets two runs on one commit produce
    # byte-identical JSONL.
    manifest = {
        "miner": {"name": miner.name, "version": miner.version},
        "tool_version": __version__,
        "repo": {"commit": ctx.commit, "branch": ctx.branch},
        "counts": {
            "emitted": len(result.emitted),
            "new": len(result.newly_seen),
            "suppressed": len(result.suppressed),
            "orphans": len(result.orphans),
            "vanished": len(result.vanished),
        },
        "options": ctx.options,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (args.out_dir / f"{miner.name}.manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if args.format in ("summary", "both"):
        print(f"candidates -> {out_path}  ({'changed' if out_changed else 'unchanged'})")
        print(f"ledger     -> {ledger_path}  ({'changed' if ledger_changed else 'unchanged'})")
    return EXIT_OK


def cmd_harvest(args: argparse.Namespace) -> int:
    module = harvesters.get(args.source)
    if module.COST_CLASS is CostClass.A_API:
        return _harvest_remote(args, module)
    return _harvest_local(args, module)


def _harvest_local(args: argparse.Namespace, module) -> int:
    # --repo defaults to an owner/name string for remote corpora; a local corpus
    # wants a checkout, so fall back to the enclosing repo unless --repo-path set one.
    root = (args.repo if isinstance(args.repo, Path) else DEFAULT_REPO_ROOT).resolve()
    if not root.is_dir():
        print(f"error: repo root does not exist: {root}", file=sys.stderr)
        return EXIT_ERROR
    print(f"harvesting {args.source} from {root}")
    records, stats = module.harvest(root, limit=args.limit if args.limit > 0 else 0)
    return _finish_harvest(args, records, stats, extra={})


def _harvest_remote(args: argparse.Namespace, module) -> int:
    token = discover_token()
    if not token and not args.allow_unauthenticated:
        print(
            "error: no GitHub token found.\n"
            "  Unauthenticated access is capped at 60 requests/hour, which is far\n"
            "  too few for a useful harvest, and a truncated corpus is worse than\n"
            "  none. Run `gh auth login`, or set GITHUB_TOKEN.\n"
            "  To proceed anyway on a tiny sample, pass --allow-unauthenticated.",
            file=sys.stderr,
        )
        return EXIT_ERROR

    client = GitHubClient(
        token=token,
        cache_dir=None if args.no_cache else args.out_dir / ".cache" / "github",
    )
    print(
        f"harvesting {args.source} from {args.repo} "
        f"({'authenticated' if client.authenticated else 'UNAUTHENTICATED'}, "
        f"limit {args.limit})"
    )

    records, stats = module.harvest(client, args.repo, limit=args.limit)
    return _finish_harvest(
        args,
        records,
        stats,
        extra={"api requests": client.requests_made, "cache hits": client.cache_hits},
        client=client,
    )


def _finish_harvest(
    args: argparse.Namespace, records, stats, *, extra: dict, client=None
) -> int:
    print("\n" + "-" * 60)
    for key, value in stats.to_json().items():
        print(f"  {key:<32} {value}")
    for key, value in extra.items():
        print(f"  {key:<32} {value}")
    print("-" * 60)

    if client is not None and client.truncated:
        print(
            f"\nWARNING: pagination stopped at the page cap for "
            f"{len(client.truncated)} listing(s). The corpus is a partial view -- "
            f"raise the cap or narrow --repo before drawing conclusions about coverage."
        )
    merged = getattr(stats, "prs_merged", None)
    if merged is not None and merged < args.limit:
        print(
            f"\nNote: --limit was {args.limit} but only {merged} merged PRs "
            f"were reachable in {stats.prs_listed} closed PRs listed."
        )

    if not records:
        print("\nNothing survived filtering.")
        return EXIT_OK

    if hasattr(records[0], "classification"):
        by_class = collections.Counter(r.classification for r in records)
        print(f"\n{len(records)} failed run(s) by classification:")
        for classification, count in by_class.most_common():
            print(f"    {count:>4}  {classification}")
    elif hasattr(records[0], "author"):
        by_author = collections.Counter(r.author for r in records)
        print(f"\n{len(records)} comments from {len(by_author)} maintainers")
        for author, count in by_author.most_common(8):
            print(f"    {count:>4}  {author}")
    else:
        print(f"\n{len(records)} page(s) to read, most-churned first:")
        for record in records[:15]:
            print(
                f"    {record.signature_change_count:>4} sig changes  "
                f"doc {record.doc_last_date} -> code {record.code_last_date}  "
                f"{record.page}"
            )

    if args.dry_run:
        print("\nDry run: no files written.")
        return EXIT_OK

    out_path = args.out_dir / f"{args.source}.corpus.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_json(r.to_record()) + "\n" for r in records)
    changed = _write_if_changed(out_path, payload)
    print(f"\ncorpus -> {out_path}  ({'changed' if changed else 'unchanged'})")
    print("This corpus is the input to the clustering step, which runs downstream.")
    return EXIT_OK


def _corpus_path(args: argparse.Namespace, source: str) -> Path:
    return args.out_dir / f"{source}.corpus.jsonl"


def _load_corpus(path: Path) -> list[dict]:
    records = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise OSError(f"{path}:{lineno}: corrupt corpus record: {exc}") from exc
    return records


def cmd_corpus_list(args: argparse.Namespace) -> int:
    print(f"corpora in {args.out_dir}\n")
    found = False
    for source in sorted(harvesters.REGISTRY):
        path = _corpus_path(args, source)
        module = harvesters.REGISTRY[source]
        if path.is_file():
            found = True
            count = len(_load_corpus(path))
            print(f"  {source:<18} {count:>4} records  [{module.COST_CLASS.value}]")
        else:
            print(f"  {source:<18}    - not harvested yet  [{module.COST_CLASS.value}]")
            print(f"  {'':<18}      run: miner.py harvest --source {source}")
    if not found:
        print("\nNothing harvested yet.")
    return EXIT_OK


def cmd_corpus_show(args: argparse.Namespace) -> int:
    harvesters.get(args.source)  # unknown name is a different error to un-harvested
    path = _corpus_path(args, args.source)
    if not path.is_file():
        print(
            f"error: no corpus at {path}\n"
            f"  run: miner.py harvest --source {args.source}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(report.render_corpus(_load_corpus(path), corpus=args.source, limit=args.limit))
    return EXIT_OK


def _ledger_names(args: argparse.Namespace) -> list[str]:
    """Every miner with a ledger, plus every registered miner.

    Defaulting `--miner` to a single name used to hide the other ledgers
    entirely, which made a populated ledger look empty.
    """
    names = set(miners.REGISTRY)
    if args.ledger_dir.is_dir():
        names.update(p.stem for p in args.ledger_dir.glob("*.jsonl"))
    return sorted(names)


def cmd_ledger_show(args: argparse.Namespace) -> int:
    names = [args.miner] if args.miner else _ledger_names(args)
    for name in names:
        ledger = Ledger.load(args.ledger_dir / f"{name}.jsonl")
        if args.state:
            entries = ledger.by_state(State(args.state))
            title = f"ledger: {name} [state={args.state}]"
        else:
            entries = ledger.sorted_entries()
            title = f"ledger: {name} (all states)"
        print(report.render_ledger(entries, title=title))
    return EXIT_OK


def _locate(args: argparse.Namespace) -> tuple[Path, Ledger]:
    """Find the ledger holding this fingerprint.

    Searching every ledger rather than assuming one matters: a fingerprint is
    globally unique, and guessing the wrong file reported "unknown fingerprint"
    for an entry that plainly existed -- on `decline`, the operation this whole
    tool is built around.
    """
    if args.miner:
        path = args.ledger_dir / f"{args.miner}.jsonl"
        return path, Ledger.load(path)

    matches = []
    for name in _ledger_names(args):
        path = args.ledger_dir / f"{name}.jsonl"
        ledger = Ledger.load(path)
        if ledger.get(args.fingerprint) is not None:
            matches.append((path, ledger))
    if not matches:
        raise LedgerError(
            f"fingerprint not found in any ledger: {args.fingerprint}\n"
            f"  searched: {', '.join(_ledger_names(args)) or '(none)'}"
        )
    if len(matches) > 1:
        raise LedgerError(
            f"fingerprint present in {len(matches)} ledgers; disambiguate with --miner"
        )
    return matches[0]


def cmd_ledger_state(args: argparse.Namespace) -> int:
    path, ledger = _locate(args)
    entry = ledger.set_state(
        args.fingerprint,
        State(args.state),
        reason=getattr(args, "reason", None),
        decided_by=getattr(args, "by", None),
    )
    ledger.save(path)
    print(f"{entry.fingerprint}\n  -> {entry.state.value}")
    if entry.reason:
        print(f"  reason: {entry.reason}")
    if entry.state is State.DECLINED:
        print("  This candidate is now suppressed permanently and will not be re-proposed.")
    return EXIT_OK


# ---------------------------------------------------------------- plumbing


_COST_CLASS_BLURB = {
    CostClass.A_API: "remote API only, no checkout",
    CostClass.B_SOURCE: "needs a checkout, no build",
    CostClass.C_BUILD: "needs a Gradle build",
}


def _write_if_changed(path: Path, payload: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return False
    path.write_text(payload, encoding="utf-8")
    return True


def _add_common(sub: argparse.ArgumentParser) -> None:
    sub.add_argument(
        "--ledger-dir",
        type=Path,
        default=DEFAULT_LEDGER_DIR,
        help="directory holding per-miner ledger files (default: %(default)s)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="miner.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"candidate-miner {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_list = subparsers.add_parser("list", help="list registered miners by cost class")
    p_list.set_defaults(handler=cmd_list)

    p_run = subparsers.add_parser("run", help="run a miner")
    p_run.add_argument("--miner", required=True, help="miner name (see `list`)")
    p_run.add_argument("--repo", type=Path, default=DEFAULT_REPO_ROOT, help="repo root to scan")
    p_run.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        help="report only; write no ledger or output files",
    )
    p_run.add_argument(
        "--format",
        choices=("jsonl", "summary", "both"),
        default="summary",
        help="`jsonl` writes records to stdout (default: %(default)s)",
    )
    p_run.add_argument(
        "--include-experimental",
        action="store_true",
        help="enable lower-precision extractors (off by default: precision over recall)",
    )
    p_run.add_argument(
        "--sanity-ceiling",
        type=float,
        default=0.25,
        help="max plausible unresolved rate before the miner declares itself broken",
    )
    _add_common(p_run)
    p_run.set_defaults(handler=cmd_run)

    p_harvest = subparsers.add_parser(
        "harvest", help="build a deterministic corpus for downstream analysis"
    )
    p_harvest.add_argument(
        "--source", default="review_comments", help="corpus name (default: %(default)s)"
    )
    p_harvest.add_argument(
        "--repo",
        default="spring-projects/spring-framework",
        help="owner/name for a_api corpora; ignored for local ones (see --repo-path)",
    )
    p_harvest.add_argument(
        "--repo-path",
        dest="repo",
        type=Path,
        help="checkout to scan, for b_source corpora (default: the enclosing repo)",
    )
    p_harvest.add_argument(
        "--limit", type=int, default=100, help="items to examine; 0 = no limit"
    )
    p_harvest.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p_harvest.add_argument("--dry-run", action="store_true")
    p_harvest.add_argument("--no-cache", action="store_true")
    p_harvest.add_argument(
        "--allow-unauthenticated",
        action="store_true",
        help="proceed without a token (60 req/hour; yields a truncated corpus)",
    )
    p_harvest.set_defaults(handler=cmd_harvest)

    p_corpus = subparsers.add_parser("corpus", help="inspect harvested corpora")
    corpus_subs = p_corpus.add_subparsers(dest="corpus_command", required=True)

    p_clist = corpus_subs.add_parser("list", help="which corpora have been harvested")
    p_clist.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p_clist.set_defaults(handler=cmd_corpus_list)

    p_cshow = corpus_subs.add_parser("show", help="print a harvested corpus")
    p_cshow.add_argument("--source", required=True, help="corpus name (see `corpus list`)")
    p_cshow.add_argument(
        "--limit", type=int, default=0, help="records to print; 0 = all (default)"
    )
    p_cshow.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p_cshow.set_defaults(handler=cmd_corpus_show)

    p_ledger = subparsers.add_parser("ledger", help="inspect and update candidate state")
    ledger_subs = p_ledger.add_subparsers(dest="ledger_command", required=True)

    p_show = ledger_subs.add_parser("show", help="print ledger contents")
    p_show.add_argument("--miner", help="default: every ledger")
    p_show.add_argument("--state", choices=[s.value for s in State])
    _add_common(p_show)
    p_show.set_defaults(handler=cmd_ledger_show)

    p_decline = ledger_subs.add_parser(
        "decline", help="permanently suppress a candidate (terminal)"
    )
    p_decline.add_argument("fingerprint")
    p_decline.add_argument("--reason", required=True, help="why; recorded in the ledger")
    p_decline.add_argument("--by", help="who decided")
    p_decline.add_argument("--miner", help="default: search every ledger")
    _add_common(p_decline)
    p_decline.set_defaults(handler=cmd_ledger_state, state=State.DECLINED.value)

    for name, state, blurb in (
        ("queue", State.QUEUED, "mark as queued for work"),
        ("done", State.DONE, "mark as completed"),
    ):
        p_state = ledger_subs.add_parser(name, help=blurb)
        p_state.add_argument("fingerprint")
        p_state.add_argument("--by", help="who decided")
        p_state.add_argument("--miner", help="default: search every ledger")
        _add_common(p_state)
        p_state.set_defaults(handler=cmd_ledger_state, state=state.value)

    return parser


if __name__ == "__main__":
    raise SystemExit(main())
