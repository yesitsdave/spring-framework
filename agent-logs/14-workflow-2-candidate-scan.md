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

## Live-run fix (2026-08-01): unknown-model AI-credits pricing

The first dispatched run failed in the agent step: gh-aw's firewall api-proxy
meters AI credits per model and returned
`400: Model "claude-opus-5" has no AI credits pricing` — the model is newer
than the proxy's shipped pricing table, so every request was rejected before
reaching Anthropic. The fix (per gh-aw ADR-47687, the BYOK-model escape
hatch) is the `models.default-ai-credits-pricing` frontmatter field, which
compiles into `apiProxy.defaultAiCreditsPricing` in the AWF config. Set to
Opus 5's real rates ($5/$25 per MTok, verified against current reference
material rather than memory) so the daily-credit guardrail meters honestly
instead of being pinned to zero or disabled. The engine model is now pinned
explicitly (`engine: {id: claude, model: claude-opus-5}`) rather than
inherited from the harness default, so the declared pricing and the model
that actually runs can't drift apart silently.

**Second failure, same 400 — the escape hatch is broken in awf v0.27.42, so
the real fix is a table-listed model.** The re-run provably used the new lock
(the init log's model lost its `[1m]` suffix, i.e. the explicit pin took),
yet the guard still reported no default pricing. Traced through the pinned
firewall tag: the compiled config carries `apiProxy.defaultAiCreditsPricing`,
the v0.27.42 schema accepts it, `config-mapper.ts` maps it, and
`ai-credits-guard.js` reads it from `AWF_DEFAULT_AI_CREDITS_PRICING` — but
that env var demonstrably never reaches the api-proxy container in the
compose path. Not our bug to fix. The curated pricing table at v0.27.42
(`containers/api-proxy/ai-credits-pricing.js`) lists every current Claude
model **except `claude-opus-5`** — Sonnet 5 and Fable 5 included — so the
model simply fell between firewall releases. Fix: pin
`engine.model: claude-opus-4-8` (in the table, same $5/$25 rates, and amply
capable of formatting three issues from prepared JSON) and drop the
non-functional pricing frontmatter rather than leave decoration that looks
load-bearing. Revisit Opus 5 when the pinned firewall image updates its
table. (The run "reaching detection" despite the failed agent step is just
gh-aw's threat-detection job running unconditionally afterwards — not
progress.)

## Live-run fix 2 (2026-08-01): the sanitizer eats HTML comments

The first live (unstaged) run filed all three issues correctly — except the
fingerprint marker was missing from every body, leaving a gap of blank lines
where it should have been. gh-aw's safe-output sanitizer strips HTML comments
from agent-authored content (its own footer comments are appended after
sanitization, which is why they survive). A stripped marker silently breaks
deduplication: the very next scan would have re-filed all three issues.

Fix, end to end: the marker contract is now a **visible inline-code line**
(`candidate-miner:fingerprint sha256:...`) as the body's last line.
`MARKER_RE` was loosened to match the bare token with a `\b` guard (so the
legacy comment-wrapped form still parses, and a 65-hex digest does not);
prompt, README, and tests updated (241 green, including a legacy-form
regression test); the three live issues were patched to carry the visible
marker. Verified live: a fresh `scan.py` run against the fork now reports
`mined 17, already filed 3, selected 3`, with the next batch being the three
75-scored candidates — dedupe holds and the drip-feed advances.

Lesson recorded: anything an agent writes through safe-outputs is
sanitized content — invisible metadata must not ride in HTML comments.
