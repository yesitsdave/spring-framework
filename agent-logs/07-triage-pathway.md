# 07 — The triage pathway: corpus → adjudication → ledger

Second CLI entry point, `triage.py`, closing the loop the pilot in `06` justified.
The chain is now runnable end to end from the command line:

```
harvest (deterministic)  →  packet (deterministic)  →  adjudicate (model)  →  ledger
```

## Structure decision

`triage.py` sits beside `miner.py` and shares the `candidateminer` package rather
than living in its own directory. It has to read the same ledger and the same
contract, so a separate package would mean duplication or a cross-package
dependency; a separate *binary* gets the separation without either.

**The boundary is now a checked property, not a promise.**
`test_miner_imports_nothing_llm` runs `import miner` in a subprocess and asserts
that neither `anthropic` nor the adjudication modules appear in `sys.modules`.
"The miners are deterministic" is a test, not a claim.

Layering: `adjudication.py` is deterministic (packets, verdict validation,
candidate construction) and fully tested without a model; `adjudicators.py` is the
only module that talks to one, and imports the SDK lazily.

## The fingerprint problem, and the fix

The ledger's single guarantee is that a human decline suppresses a candidate
permanently. That depends on `sha256(category ‖ path ‖ identity)` being
reproducible — so **the model must not author a candidate's identity.** Free-text
identity would mint a second fingerprint whenever the model rephrased itself,
and a declined finding would silently return.

So a verdict must:
- **cite a symbol** that appears in the packet it was given, and
- **pick a claim type** from a closed vocabulary (`wrong_count`,
  `incomplete_listing`, `stale_signature`, `removed_member`, `wrong_modifier`).

Identity is `{page}::{symbol}::{claim_type}` — stable across rephrasing, and both
halves checkable. The vocabulary is enforced twice: as an `enum` in the
structured-output JSON schema (so the API constrains generation), and again in
`_parse_verdict` on the way back, because a schema is a request, not a guarantee.

`test_identity_is_not_authored_by_the_model` pins this: same finding, entirely
different rationale, citation and confidence → same fingerprint.

## What a verdict must survive before it reaches the ledger

`validate_verdict` rejects, and `triage.py run` reports as `DISCARD`:

| Rejected | Why |
|---|---|
| a symbol not present in the packet | the model may only cite what it was shown |
| a claim type outside the vocabulary | no interpreting novel claims |
| a citation that is not `file:line` | unciteable claims are discarded, not trusted |
| a citation to a missing file, or a line past EOF | the claim is unsupported by the tree |
| an empty rationale | nothing for a reviewer to check |

Prompt-side, the bar from the pilot is encoded directly: **contradictions, not
omissions**, reject on uncertainty, and an explicit statement that rejecting is
the correct answer most of the time because a wrong accept spends the scarcest
resource on the project — maintainer review attention.

## API usage

`claude-opus-5`, structured outputs via `output_config.format` with a JSON schema,
`max_tokens=16000`, and the system prompt cached with `cache_control` (identical
across packets, so all but the first call read from cache). `stop_reason` is
checked before `content` is read, because safety classifiers can decline and leave
content empty or partial.

Server-side refusal fallbacks were **not** wired in. They are the documented
default for Opus 5 code, and the reason to skip them here is narrow: adjudicating
Spring documentation is not a category that trips the classifiers, and the beta
path would couple the tool to a beta header for no benefit on this workload. A
refusal is instead surfaced as an explicit defect naming the packet. Worth
revisiting if the corpus ever includes security-adjacent material.

## Design flaw found by the tests

`--dry-run` initially meant *both* "use the stub adjudicator" and "write nothing",
which left no way to exercise the ledger-write path without spending money — the
accept-path tests could not run at all.

Split into two flags: `--dry-run` means write nothing (matching `miner.py`), and
`--adjudicator {claude,stub}` chooses who decides, defaulting to stub under
`--dry-run` and claude otherwise. The write path is now covered by tests, and
`--dry-run` no longer quietly means two things.

## State

188 tests. The pathway runs end to end against the real 9-page corpus with the
stub adjudicator; a live run needs `pip install anthropic` plus credentials
(`ant auth login` or `ANTHROPIC_API_KEY`), neither of which is present in this
environment. Nothing else is missing — the SDK import is lazy, so `miner.py`,
`packet`, and the whole test suite work without it.

## Live run (2026-07-31, claude-opus-5)

Prediction on record before the run: 1–2 accepts of 9; `factory-nature.adoc`
should surface the `LifecycleProcessor` contradiction; `jpa.adoc` should be
rejected despite ranking first on churn.

Result: **1 accepted, 8 rejected, 0 discarded** — exactly the predicted shape,
including both named pages. Zero discards means every verdict survived
symbol/citation validation; nothing had to be dropped for fabricated evidence.

The accept is the pilot finding, independently rediscovered by a fresh context
from the packet alone:

```
[ 75] LifecycleProcessor (wrong_count)  confidence: medium
      core/beans/factory-nature.adoc — page says "adds two other methods";
      the interface declares four (onRefresh, onClose, onPause, onRestart —
      the last two added 2025-08-01, @since 7.0)
      sha256:949c3382…  →  ledger/adjudicated_docs_drift.jsonl, state: new
```

Fingerprint identical to the pilot's, as designed — the identity is
`page::symbol::claim_type`, so the independent rediscovery deduplicates
instead of duplicating.

One imperfection, recorded rather than hidden: the model cited `:436`, but the
claim sentence sits at `factory-nature.adoc:431-432`; line 436 is the adjacent
`[TIP]` block. The citation passed validation because validation checks only
that the line exists in the named file — exactly why the contract marks lines
`line_is_advisory` and excludes them from fingerprints. A reviewer landing at
436 still sees the passage; a reviewer trusting the line as exact would be five
lines off. Tightening this (require the cited line to contain the symbol) is
possible but would have discarded a *true* finding here — the wrong-count
sentence names the symbol on the previous line. Left as-is, noted as a known
looseness.

Cost: ~53k input tokens across 9 calls, system prompt cached after the first.

Accepted verdicts become ordinary candidates in
`ledger/adjudicated_docs_drift.jsonl` and are declined with the same
`miner.py ledger decline` command as any mined candidate —
`test_decline_survives_a_second_adjudication` proves the suppression holds.
