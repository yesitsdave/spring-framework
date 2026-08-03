# 16 — Workflow 4: `candidate-ledger-reconcile`, and the program complete

The last workflow of the plan of record (log 13): fold issue outcomes back
into the committed ledger. Fully deterministic — like `candidate-miner-ci`
this is plain Actions, not gh-aw; putting a model between an issue's state
field and a JSONL edit would be the hammer the brief warns about.

## Shape

`reconcile.py` (new, beside the other workflow-support CLIs; 11 new tests,
262 total) reads every candidate-miner issue and maps gestures to states:

| Issue gesture | Ledger state |
|---|---|
| open | `queued` |
| closed as completed (a merged PR's `Fixes #N` does this) | `done` |
| closed as not planned | `declined` — permanent, recording issue number and who closed it |
| reopened after done | `queued` again |
| anything vs. `declined` | **held** and reported — a veto is terminal; only a deliberate hand edit undoes it |

The workflow triggers on issue `closed`/`reopened` (filtered to the
`candidate-miner` label), plus a weekly sweep and manual dispatch, and
commits `ledger/` only when bytes changed (`Ledger.save` is a no-op on
identical content, so quiet weeks produce no commits).

## Decisions

1. **GitHub's token-event suppression is load-bearing, in our favour.**
   Issues created by the scan's safe-outputs job use `GITHUB_TOKEN`, and
   GitHub does not fire workflow triggers for such events — so scan-created
   issues cannot recursively trigger reconcile, and reconcile's own ledger
   push cannot trigger CI. The `new → queued` transitions those suppressed
   events would have driven are picked up by the weekly sweep instead.
   Human gestures (close, reopen) use a user token and fire immediately.
2. **Abort on truncated listings, before writing.** Reconciling from a
   partial issue view could miss a not-planned close — a lost veto, the
   worst failure for this tool. Same fail-loud posture as `scan.py`, but
   here the guard runs before any ledger is saved.
3. **A concurrency group serialises runs**, and the commit step rebases
   before pushing, so two rapid gestures cannot race the push.
4. **Declined conflicts are surfaced, not resolved.** If someone reopens an
   issue whose candidate was declined, the ledger holds and the run summary
   says so. The ledger's one guarantee — a decline survives everything —
   is worth an occasional stale-looking issue.

## Verification

- 262 tests green (11 new): every gesture mapping, decline provenance
  (reason + closer login), terminal-decline conflict, PR bodies ignored
  (they quote fingerprints too), unknown/ambiguous markers reported not
  fatal, truncation aborts before writing, no-op stability.
- Live run against the fork: the three open issues moved their entries
  `new → queued` in `docs_dead_ref` and `config_dead_entry` — the first
  ledger state change driven by the live pipeline, committed with this log.

## The program, complete

All four workflows of the plan of record now exist and have run live:

```
candidate-miner-ci          plain CI      the tool's own rot guard
candidate-scan              gh-aw         weekly: mine → dedupe → ≤3 issues
candidate-solve             gh-aw         label: verify → fix → prove → draft PR
candidate-ledger-reconcile  plain CI      gestures → ledger states, committed
```

The full lifecycle demonstrated end to end on the fork: mined candidate →
ranked issue (#1) → `solve-it` label → verified fix → draft PR → (on merge,
`Fixes #1` closes the issue as completed → reconcile marks the ledger
`done`). Every LLM step is bracketed by deterministic verification, every
escalation sits behind a human gesture, and every human gesture flows back
into permanent machine memory.
