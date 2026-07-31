# 12 — Review-driven hardening pass and re-harvest verification

A code review of commit `4c5f50503f` surfaced five defects worth fixing plus a
sanity-check blind spot and documentation drift. All are fixed; every fix
carries a named regression test. 226 tests (up from 209), all passing.

## The fixes

1. **Citation validation now enforces the packet** (`adjudication.py`). A
   verdict's citation was checked only for existence and line range, so it
   could name any file at all — including, via `..`, files outside the repo —
   and launder that path into the emitted candidate. Citations are now
   restricted to `Packet.citable_paths()` (the doc page and the shown source
   files), the same only-what-you-were-shown rule the symbol check already
   enforced. A citation into a shown `.java` file no longer supplies the doc
   page's locus line. The adjudicator prompt states the constraint.
2. **Harvest pagination** (`review_comments.py`, `ci_failures.py`). PR reviews,
   PR review comments, and run job listings were fetched with a single `get()`
   — one default 30-item page. All three now paginate. Test fakes deliberately
   dropped their `get()` so a regression fails loudly.
3. **docs_drift no longer drops pre-2022 pages** (`docs_drift.py`). The
   commit-date index only covered `--since 2022-01-01`; a page whose last
   substantive edit predated it resolved to no date and was silently skipped —
   excluding exactly the longest-untouched pages. Older commits are now
   resolved lazily, with the same bulk-sweep check applied. Git subprocesses
   are locale-pinned (`LC_ALL=C`) so the `--shortstat` parse cannot be broken
   by a localized git.
4. **GitHub cache namespacing** (`github.py`). `get()` and `paginate()` shared
   one `sha256(url)` cache namespace while storing different shapes; paginated
   pages now live under `.page.json`.
5. **Credential boundary on the JSON path** (`github.py`). `_request` used
   urlopen's default redirect handling, which re-sends the original headers —
   Bearer token included — to whatever host a Location header names. It now
   follows redirects manually with the token re-decided per hop, exactly as
   `_request_text` already did. 303/308 handled on both paths.
6. **Zero-sample guards** — the mirror image of the sanity ceilings, which all
   guarded against too *many* findings while a broken extractor emitting
   *nothing* read as a clean bill of health. `docs_dead_ref` defects on zero
   extracted references across ≥50 pages; `config_dead_entry` defects when a
   config file contains its marker but parses to no entries; `flaky_test`
   defects on an empty corpus file.
7. **Docs re-synced**: README cost-class table (`flaky_test` is `a_api`), three
   miners and three corpora, the `adjudicated_docs_drift.jsonl` ledger file
   documented, and the golden-repo test described honestly (it pins a known-dead
   canary set in addition to properties). AGENTS.md now states how to verify
   its citations given that `out/` is derived and not committed.

## Re-harvest verification (2026-07-31, post-fix)

The pagination and history-cutoff fixes could in principle have changed the
corpora the committed evidence rests on. Measured, not assumed:

- `review_comments` at `--limit 1200`: **byte-identical** to the committed
  corpus (34 changes-requested PRs, 131 seen, 61 kept). No PR in the reachable
  window had >30 reviews or comments, so the single-page bug had cost nothing
  here — and determinism held across the rewrite.
- `ci_failures` at `--limit 100`: zero overlapping records changed (the jobs
  bug also had no practical impact on this data). Six April–May runs aged out
  of the per-push workflow's newest-100 window; the flaky_test miner still
  emits the same two candidates with the same fingerprints — the top one now
  shows 2 in-window occurrences instead of 3, scoring 75 rather than 100. The
  committed ledger evidence remains accurate for its recorded window.
- `docs_drift`: **unchanged** (same 9 pages). The Antora migration means every
  current page's history at its current path postdates 2022, so the pre-2022
  fix is correct in principle but changes nothing on this tree today.

**Net: no new candidates, no lost candidates, no evidence invalidated.**
AGENTS.md's rules and the final report's inventory stand unmodified. The review
also confirmed the load-bearing claims: miner.py's import graph cannot reach a
model, declines are terminal, and candidate identity is never model-authored.
