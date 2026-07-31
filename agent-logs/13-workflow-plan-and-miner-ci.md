# 13 — Deliverable 2 plan, and workflow 1 (`candidate-miner-ci`)

## The plan of record

GitHub issues become the **human interface**; the ledger stays the **machine
memory**. Neither replaces the other. Issues-only would reinvent the ledger in
issue-body markers to keep declines terminal; ledger-only leaves maintainers
staring at a JSONL file nobody reads. So sync is explicit and one gesture maps
to one state: close-as-not-planned → `declined` (permanent), linked PR merged →
`done`.

Four workflows, built in rollout order, deterministic-first:

| # | Workflow | Trigger | Determinism split |
|---|---|---|---|
| 1 | `candidate-miner-ci` | push/PR touching the tool | fully deterministic — the 226-test suite. The tool that guards against rot gets a rot guard. |
| 2 | `candidate-scan` | weekly + `workflow_dispatch` | deterministic pre-step runs the miners; **zero new candidates → exit before the agent starts** (zero tokens on idle weeks). Otherwise a thin agent writes issues via `create-issue` safe-output (`max: 3`, title-prefix, fingerprint as HTML comment). Ledger `new → queued` with issue number, committed by the workflow. |
| 3 | `candidate-solve` | label command (`solve-it`) | agent brackets: deterministic re-verify at HEAD before (miner re-run for that fingerprint), fix following AGENTS.md, deterministic re-run after proving the candidate is *gone*, then `create-pull-request` (`draft: true`, `max: 1`). |
| 4 | `ledger-reconcile` | issue close / PR merge | fully deterministic script mapping issue outcomes to ledger states. No LLM. May fold into #2 as a pre-step. |

Rollout safety: `staged: true` on safe-outputs for first runs, `stop-after:`
as a dead-man's switch, issue caps to drip-feed rather than flood. The steady
state of a precision-first miner on a well-kept repo is "found nothing", and
the design makes that state cost nothing.

Known tension, stated rather than hidden: most of the current 18 candidates are
*stock* (drain once); only `flaky_test` and `docs_drift` are renewable *flow*.
Cadence (weekly scan, monthly triage) is set for the flow, not the stock.

## Workflow 1: decisions

**Plain GitHub Actions, not gh-aw.** The suite is deterministic; putting an
agent in front of `python -m unittest` would be using the hammer the brief
warns about. gh-aw enters with workflow 2, where prose-writing is the job.

- **No upstream repo guard.** The repo's other workflows carry
  `if: github.repository == 'spring-projects/spring-framework'` to spare forks
  the heavy builds. Inverted here: the tool lives on the fork, and the path
  filter (`tools/candidate-miner/**` + the workflow file itself) already
  scopes it. Copying the guard would have disabled the workflow everywhere.
- **System `python3`, no `setup-python`.** The tool is stdlib-only, 3.11+.
  Testing against whatever `ubuntu-latest` ships *is* the honest test, and it
  removes an action-version to keep current.
- **`actions/checkout@v6`**, matching house convention in the sibling
  workflows.

## Shallow-checkout verification

CI checkouts are depth-1, and the golden-repo tests mine the actual tree with
`git log` evidence queries — the one place CI genuinely differs from a dev
checkout. Verified by running the full suite in a fresh `--depth 1` clone:

- **226/226 pass**, all 10 golden tests running, none skipped.
- **2.9s vs 19.7s** against full history. The gap is `last_deletion_of`
  walking ~100k commits per dead reference; on a shallow clone those queries
  return empty and `gitutil` degrades to `None` exactly as its contract says
  (`removed_in: null` in evidence, nothing pinned by the golden tests).
  CI landed on the fast path by design, not luck.
