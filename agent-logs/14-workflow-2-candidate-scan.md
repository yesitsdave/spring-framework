# 14 — Workflow 2: `candidate-scan` (gh-aw)

The first agentic workflow: weekly, deterministic mining feeding a thin
issue-filing agent through gh-aw safe-outputs. Compiled with gh-aw v0.83.4.

## Shape

```
candidate-scan.md (frontmatter steps, deterministic)      (agent, LLM)
  checkout (full history, credentials not persisted)
  actions/cache restore of out/.cache/github
  scan.py:                                            →   read proposable.json
    harvest ci_failures (skip flaky_test on failure)  →   write ≤3 issues via
    run all miners --dry-run                          →   create-issue safe
    drop candidates with an existing issue (any state)     output (staged)
    rank by score, cap at 3 → out/proposable.json
```

`scan.py` is new, lives beside `miner.py`/`triage.py`, reuses the tested CLI
surface in-process, and is covered by 13 new tests (239 total). The agent's
authority is exactly the file's contents: it may read the repo for context but
may not add, reorder, or re-judge candidates, and must copy fingerprints
byte-for-byte into the issue-body marker that all future dedup hangs on.

## Decisions

1. **Issues dedupe by fingerprint marker, ledger untouched by CI.** The scan
   never writes the ledger (`--dry-run` mining) and never commits. Candidates
   already filed are recognised by scanning issue bodies for
   `<!-- candidate-miner:fingerprint sha256:... -->` across **all** issue
   states — so *closing an issue is already a working veto* even before the
   reconcile workflow exists. Ledger declines stay the permanent tool-level
   suppression. No schema change to the ledger was needed; workflow 4 will
   sync issue outcomes back to it.
2. **The issue listing is fetched uncached.** The GitHubClient caches
   permanently by design (harvest reproducibility); a cached issue listing
   would miss issues filed after the cache was written and re-propose their
   candidates. `scan.py` passes `cache_dir=None` for this one query and
   aborts if pagination truncates — a partial listing fails loudly rather
   than double-filing.
3. **Deviation from the plan of record (log 13): idle weeks cost one small
   agent turn, not zero tokens.** The compiled lock file shows the agent job
   gates only on gh-aw's own guardrails; there is no hook for skipping the
   engine on a custom step's output, and the activation job is a slim runner
   with no checkout. The two honest routes to a true zero-token gate — a
   separate plain workflow that `workflow_dispatch`es this one, or mining
   twice in the activation job — both trade real DX complexity for a few
   cents a week. Single workflow wins; the prompt's first rule is "empty
   file → stop immediately".
4. **Harvest failure degrades, miner failure aborts.** Losing the network
   loses this week's flaky-test proposals (recorded as a warning in
   `proposable.json` and the step summary), not the docs/config proposals.
   A miner exiting 2 propagates as exit 2: a defect means humans look.
5. **Frontmatter `cache:` was wrong for this job — caught by reading the
   lock file.** The compiler appends that step *after* custom steps, so the
   restore would land after the harvest that needs it. Replaced with an
   explicit `actions/cache` step between checkout and scan (run-id key,
   prefix restore, so the cache both restores and re-saves as it grows).
6. **Staged maiden run.** `safe-outputs: staged: true` renders would-be
   issues into the run summary instead of creating them. It comes off after
   one reviewed preview.

Things the compiler enforced that were worth keeping, not fighting: checkout
in the agent job requires `persist-credentials: false` (the git token would
otherwise sit in `.git/config` inside the agent's sandbox), every action gets
SHA-pinned, and the `ANTHROPIC_API_KEY` secret had to be explicitly approved
into the lock file.

## Verification

- 239 tests green, including end-to-end scan tests that stub the harvest
  offline (the local environment holds live credentials; a test that quietly
  goes to the network when they exist is the exact environment-dependence
  this tool bans).
- Live rehearsal against the real tree and the real fork:
  `./scan.py --repo-slug yesitsdave/spring-framework --max 3` → mined 17
  (the full open inventory), 0 filed, selected
  `PropertySourcesPlaceholderConfigurer` (85),
  `Target_BytecodeProviderInitiator` suppression (77), `spring-boot-issues`
  antora attribute (75). The strongest candidate in the corpus leads the
  first batch, which is what the ranking is for.

## To go live

Push, add the `ANTHROPIC_API_KEY` repo secret, `workflow_dispatch` a run,
review the staged preview in the run summary, then delete `staged: true`,
recompile, and let the schedule take it.
