# 15 — Workflow 3: `candidate-solve` (gh-aw)

The PR-authoring workflow: a human applies the `solve-it` label to a
candidate-miner issue, and an agent — bracketed by deterministic verification
on both sides — makes the minimal fix and opens a draft PR. Human pull, not
machine push: nothing happens until someone asks.

## Shape

```
label `solve-it` applied to an issue
  └─ pre-step (deterministic): solve_target.py --issue N
       issue body → fingerprint marker → committed ledger → owning miner
       → re-run that miner at HEAD → verdict in out/solve_target.json
  └─ agent:
       verdict != confirmed → one explanatory comment, stop
       confirmed → read AGENTS.md → minimal fix per category
       → solve_target.py --fingerprint <fp> (offline) → must say `gone`
       → draft PR via create-pull-request (staged for the maiden run)
```

The same tool proves the candidate's presence before the fix and its absence
after — "the tool that found the bug verifies the fix", now enforced in CI
rather than described in a plan.

## `solve_target.py`

New verifier beside `miner.py`/`scan.py`/`triage.py`; 10 new tests (251
total). Verdicts are data, not errors — every verdict exits 0 and lands in
`out/solve_target.json`; only infrastructure failures (exit 1) and miner
defects (exit 2) are errors. The verdict vocabulary is closed:

| verdict | meaning | agent's move |
|---|---|---|
| `confirmed` | candidate exists at HEAD; record attached | fix it |
| `gone` | miner no longer emits it | pre: issue is stale, comment; post: fix proven |
| `declined` | human ledger veto (reason attached) | comment, never solve |
| `unsupported` | corpus miner (flaky_test) or adjudicated ledger — historical evidence, "gone" unprovable | comment, human takes it |
| `no_marker` / `ambiguous_marker` / `unknown_fingerprint` | issue not traceable to one candidate | comment what is wrong |

Design points:

- **The ledger names the owning miner.** A fingerprint alone does not say
  which miner produced it; the committed ledger file it sits in does. Only
  that one miner re-runs — not the whole scan.
- **`--fingerprint` mode is fully offline.** The post-fix check needs no
  token and no issue fetch, so the agent's bash allowlist is a single
  command and the verification cannot be poisoned by anything network-side.
- **Scope guard by category.** Only the two mechanical shapes are solvable:
  an `unambiguous` relocation (one-token package fix) and a dead config
  entry (line removal). Anything editorial — ambiguous relocations,
  `illustrative_only` doc examples, section removals needing prose judgement
  — the prompt orders the agent to revert, explain, and stop. The miner's
  precision-first posture carries into the solver.

## AGENTS.md promoted to the repo root

The draft guidance (log 09) moved from `tools/candidate-miner/guidance/` to
`/AGENTS.md` — the solve agent is its first real consumer, instructed to read
and obey it (presume-deliberate, the vendored-code prohibition, one concern
per PR). This enacts final-report recommendation 2 on the fork and closes the
brief's "agentic guidance" loop: the rules mined from maintainer review
comments now constrain the agent writing PRs.

## Workflow mechanics worth recording

- `label_command` is a one-shot trigger: gh-aw removes the label after
  firing, so re-labelling re-runs. The compiled gate checks
  `github.event.label.name == 'solve-it'`.
- The compiler auto-extracted `${{ github.event.issue.number }}` from the
  run script into an env var to prevent shell injection — unprompted, and
  exactly right.
- `engine.model` is deprecated in gh-aw v0.83.4; both workflows migrated to
  the top-level `model:` field.
- Agent tools: `edit` plus a bash allowlist containing exactly one command
  (the offline verifier). The PR itself is created by the safe-outputs job
  from the agent's diff — the agent never touches git or tokens.
- `create-pull-request` is `draft: true`, `max` defaults to 1, and the
  maiden run is `staged: true`, same rollout ritual as candidate-scan.

## Verification

- 251 tests green (10 new), including: confirmed→gone round trip on a
  fixture repo (mine, break the reference, fix the page, re-verify), every
  verdict in the vocabulary, defect propagation, and the offline
  `--fingerprint` mode running with no client at all.
- Live rehearsal: `./solve_target.py --repo-slug yesitsdave/spring-framework
  --issue 1` → `confirmed`, `docs_dead_ref`,
  `org.springframework.beans.factory.config.PropertySourcesPlaceholderConfigurer`,
  full candidate record attached — the exact input the agent will receive.
- `solve-it` label created on the fork.

## To go live

Push, apply `solve-it` to issue #1, review the staged PR preview in the run
summary, then remove `staged: true`, recompile, re-label. Issue #1 is the
designed first target: the one-token package fix that log 02 called "the one
PR to open if only one is ever opened".

## Live-run fix (maiden run): bash allowlist needs `:*`, not ` *`

The first labelled run confirmed the design and found one authoring bug. The
verdict came back `confirmed`, the agent made the correct one-token edit —
then the mandated post-fix verification was auto-denied: the frontmatter
entry `"python3 tools/candidate-miner/solve_target.py *"` compiles to
`Bash(python3 tools/candidate-miner/solve_target.py)` with the shell-glob
suffix silently dropped. Claude Code permission patterns want `:*` to admit
arguments (compare the built-ins: `Bash(git add:*)`), so only the bare
command was allowed and `--fingerprint`, even `--help`, were denied.

Two things worth keeping from this run:

1. **The agent's failure behaviour was exactly what the prompt demanded.**
   Unable to produce the `gone` proof, it did not fabricate the verdict or
   open an unverified PR: it reverted its edit, verified the tree was clean,
   posted one explanatory comment, and filed a `missing_tool` report naming
   the precise allowlist fix. The verification gate held under pressure,
   which is the whole point of the gate.
2. **Authoring lesson:** after compiling, grep the lock file's
   `--allowed-tools` list for every bash entry you declared — the compiler
   normalises entries and drops what it does not understand, silently. The
   cache-ordering bug (log 14) and this one were both caught only by reading
   the compiled artifact.

Fixed to `"python3 tools/candidate-miner/solve_target.py:*"`; the compiled
pattern now carries the wildcard.
