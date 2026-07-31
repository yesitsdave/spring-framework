"""Human-readable run summary.

The JSONL is for machines. This is for the person deciding whether any of it is
worth a pull request, so it leads with the evidence a reviewer would ask for
first: what is referenced, where, and when it stopped existing.
"""

from __future__ import annotations

import shutil
from typing import Sequence

from .contract import Candidate, State
from .ledger import Ledger, MergeResult

_RULE = "-"


def _width() -> int:
    return min(shutil.get_terminal_size((100, 24)).columns, 100)


def _hr(char: str = _RULE) -> str:
    return char * _width()


def render(
    *,
    miner_name: str,
    result: MergeResult,
    ledger: Ledger,
    commit: str,
    branch: str,
    index_size: int,
    dry_run: bool,
) -> str:
    lines: list[str] = []
    add = lines.append

    add(_hr("="))
    add(f"candidate-miner: {miner_name}")
    add(f"  commit {commit[:12]} on {branch}   |   {index_size} type FQNs indexed")
    if dry_run:
        add("  DRY RUN - no files written")
    add(_hr("="))
    add("")

    total_known = len(ledger)
    add(
        f"{len(result.emitted)} candidate(s) to review   "
        f"({len(result.newly_seen)} new, "
        f"{len(result.emitted) - len(result.newly_seen)} carried over)"
    )
    if result.suppressed:
        add(f"{len(result.suppressed)} suppressed by a previous decline")
    add(f"{total_known} fingerprint(s) tracked in the ledger")
    add("")

    if result.emitted:
        add(_hr())
        for candidate in sorted(
            result.emitted, key=lambda c: (-c.score.value, c.fingerprint)
        ):
            lines.extend(_render_candidate(candidate, ledger))
        add(_hr())
        add("")

    if result.orphans:
        add("POSSIBLE RENAMES - needs a human")
        add(
            "  These fingerprints have a decision recorded, were not re-found, and "
            "their file no longer exists."
        )
        add(
            "  Most likely the file moved. Re-decide them against the new path; the "
            "tool will not guess,"
        )
        add("  because silently carrying a decline to the wrong file fails invisibly.")
        for entry in result.orphans:
            add(f"    [{entry.state.value}] {entry.path}")
            add(f"        {entry.identity}")
            add(f"        {entry.fingerprint}")
        add("")

    if result.vanished:
        add(f"RESOLVED UPSTREAM - {len(result.vanished)} tracked candidate(s) no longer present")
        add("  Their files still exist, so the reference itself was fixed or removed.")
        for entry in result.vanished:
            add(f"    [{entry.state.value}] {entry.identity}  ({entry.path})")
        add("")

    if not result.emitted and not result.orphans:
        add("Nothing to review.")
        add("")

    return "\n".join(lines)


def _render_candidate(candidate: Candidate, ledger: Ledger) -> list[str]:
    entry = ledger.get(candidate.fingerprint)
    state = entry.state.value if entry else State.NEW.value
    evidence = candidate.evidence
    out: list[str] = []
    add = out.append

    add("")
    add(f"[{candidate.score.value:>3}] [{state}] {evidence.get('reference', candidate.identity)}")
    add(f"      {candidate.locus.path}:{candidate.locus.line}")

    if "removed_in" in evidence:
        removed = evidence["removed_in"]
        if removed:
            add(f"      removed {removed['date']} in {removed['commit'][:12]}")
        else:
            add("      no deletion commit found (may predate history or never have existed)")

    out.extend(_DETAIL_RENDERERS.get(candidate.category, lambda _: [])(evidence))

    add(f"      score {candidate.score.value} = " + " + ".join(
        f"{k}:{v}" for k, v in sorted(candidate.score.inputs.items()) if v
    ))
    add(f"      {candidate.fingerprint}")
    return out


def _render_config_detail(evidence: dict) -> list[str]:
    out: list[str] = []
    kind = evidence.get("config_kind", "?")
    if kind == "antora_attribute":
        out.append(f"      {kind}: declared but never referenced as {{{evidence['pattern']}}}")
        out.append(f"          {evidence.get('value', '')[:88]}")
        return out
    if evidence.get("names_a_literal"):
        out.append(f"      {kind}: names a specific target that no longer exists")
    else:
        out.append(f"      {kind}: pattern matches nothing in the tree")
    out.append(f"          {evidence.get('raw_line', '')[:88]}")
    return out


def _render_docs_detail(evidence: dict) -> list[str]:
    out: list[str] = []
    add = out.append

    public = evidence.get("relocated_to_public_api") or []
    relocated = evidence.get("relocated_to") or []
    confidence = evidence.get("relocation_confidence", "none")

    if confidence == "unambiguous":
        add(f"      relocated -> {public[0]}  (sole match, published API; one-token fix)")
    elif confidence == "ambiguous":
        add(
            f"      {len(relocated)} same-named types exist - a human must pick, "
            f"or none may be right:"
        )
        for target in relocated[:3]:
            marker = "public" if target in public else "test-only"
            add(f"          {target}  [{marker}]")
        if len(relocated) > 3:
            add(f"          ... and {len(relocated) - 3} more")
    elif confidence == "test_only":
        add(f"      ILLUSTRATIVE ONLY - same-named type(s) exist, all test-only:")
        for target in relocated[:2]:
            add(f"          {target}")
        add("      Docs point at something never published; fix is editorial, not mechanical.")

    occurrences = evidence.get("occurrences") or []
    add(f"      {len(occurrences)} occurrence(s) via {'+'.join(evidence['extractors'])}")
    for occurrence in occurrences[:3]:
        add(f"          {occurrence['line']:>5}: {occurrence['text'][:80]}")
    if len(occurrences) > 3:
        add(f"          ... and {len(occurrences) - 3} more")
    return out


_DETAIL_RENDERERS = {
    "docs.dead_api_reference": _render_docs_detail,
    "config.dead_entry": _render_config_detail,
}


def render_corpus(records: Sequence[dict], *, corpus: str, limit: int = 0) -> str:
    """Human-readable view of a harvested corpus.

    Harvesters emit reading material rather than candidates, so this is the
    counterpart to `render_ledger` -- without it a corpus could only be read as
    raw JSONL, which is a poor way to look at prose.
    """
    lines = [_hr("="), f"corpus: {corpus}  ({len(records)} records)", _hr("="), ""]
    if not records:
        lines += ["(empty)", ""]
        return "\n".join(lines)

    shown = records[:limit] if limit else records
    renderer = _CORPUS_RENDERERS.get(corpus, _render_generic_record)
    for record in shown:
        lines.extend(renderer(record))
    if len(shown) < len(records):
        lines.append(f"... {len(records) - len(shown)} more (raise --limit to see them)")
        lines.append("")
    return "\n".join(lines)


def _render_docs_drift_record(record: dict) -> list[str]:
    out = [
        f"[{record['signature_change_count']:>3} sig changes] {record['page']}",
        f"      doc last {record['doc_last_substantive']['date']}"
        f"  ->  code last {record['code_last_substantive']['date']}",
        f"      {record['referenced_type_count']} framework types referenced"
        + ("  (file list truncated)" if record.get("files_truncated") else ""),
    ]
    for change in record.get("signature_changes", [])[:4]:
        out.append(f"          {change[:84]}")
    remaining = len(record.get("signature_changes", [])) - 4
    if remaining > 0:
        out.append(f"          ... and {remaining} more")
    out.append("")
    return out


def _render_review_comment_record(record: dict) -> list[str]:
    body = " ".join((record.get("body") or "").split())
    out = [
        f"PR #{record['pr']}  [{record['author']}]  {record['path'].split('/')[-1]}",
        f"      {body[:150]}",
    ]
    if len(body) > 150:
        out.append(f"      {body[150:290]}")
    out.append(f"      {record.get('url', '')}")
    out.append("")
    return out


def _render_generic_record(record: dict) -> list[str]:
    keys = [k for k in sorted(record) if k not in ("schema_version", "corpus")]
    return [f"  {k}: {str(record[k])[:90]}" for k in keys] + [""]


def _render_ci_failure_record(record: dict) -> list[str]:
    date = (record.get("run_started_at") or "")[:10]
    out = [
        f"[{record.get('classification', '?'):>7}] {date}  {record.get('workflow', '')}"
        f"  run {record.get('run_id', '')}",
    ]
    tasks = record.get("failed_tasks") or []
    if tasks:
        out.append(f"      failed: {', '.join(tasks[:4])}")
    for test in record.get("failing_tests", [])[:4]:
        cause = f"  ({test['cause']})" if test.get("cause") else ""
        out.append(f"        {test['test_class']}.{test['method']}{cause}")
    if not record.get("log_available"):
        out.append("      (log expired -- classification unavailable)")
    if record.get("scan_url"):
        out.append(f"      {record['scan_url']}")
    out.append("")
    return out


_CORPUS_RENDERERS = {
    "ci_failures": _render_ci_failure_record,
    "docs_drift": _render_docs_drift_record,
    "review_comments": _render_review_comment_record,
}


def render_ledger(entries: Sequence, *, title: str) -> str:
    lines = [_hr("="), title, _hr("="), ""]
    if not entries:
        lines.append("(empty)")
        lines.append("")
        return "\n".join(lines)
    for entry in entries:
        lines.append(f"[{entry.state.value:>8}] {entry.identity}")
        lines.append(f"           {entry.path}")
        if entry.reason:
            lines.append(f"           reason: {entry.reason}")
        if entry.decided_by:
            lines.append(f"           by: {entry.decided_by}")
        lines.append(f"           {entry.fingerprint}")
        lines.append("")
    return "\n".join(lines)
