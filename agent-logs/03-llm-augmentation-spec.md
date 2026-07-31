# 03 — LLM-augmented mining: design spec

Status: **design only.** Nothing here is built. The two shipped miners
(`docs_dead_ref`, `config_dead_entry`) remain fully deterministic and must stay
that way.

## The architectural rule

An LLM must never perform **discovery**. Over 845k LOC it is expensive,
irreproducible, and has worse recall than `grep`. It is good at **adjudication**
(is this deviation deliberate?) and **drafting** (write the replacement prose).

> Deterministic pre-filter with a **measured** reduction ratio → LLM judges each
> survivor → verdict cached by fingerprint.

Cost is therefore `O(candidates)`, not `O(LOC)`. Three consequences:

1. **No pre-filter, no LLM stage.** Every spec below states its reduction ratio
   and how it was measured. A stage whose ratio is unmeasured does not ship.
2. **Verdicts are cached in the ledger.** Fingerprints are already content-
   addressed (`sha256(category ‖ path ‖ identity)`), so a verdict is recomputed
   only when the evidence itself changes. This falls out of the existing design
   at no cost and is the single biggest saving available.
3. **The LLM is a gate, not a generator.** It should be prompted to *reject*, with
   rejection as the default on uncertainty. Given 124 untriaged PRs upstream,
   a stage that increases volume is harmful regardless of patch quality.

### Contract extension

Add one optional block to the candidate record. Absent means never adjudicated.

```json
"triage": {
  "verdict": "accept | reject | uncertain",
  "confidence": "high | medium | low",
  "rationale": "one sentence, must cite file:line",
  "model": "<model-id>",
  "evidence_digest": "sha256:…",
  "prompt_version": 3
}
```

`evidence_digest` hashes the evidence block the verdict was formed against. If the
evidence changes the verdict is invalidated; if not, it is reused forever.
`prompt_version` allows a deliberate global re-run when the prompt changes.

**A verdict never overrides a human `declined`.** The ledger state machine already
makes decline terminal; the triage block is advisory input to a human, not a state
transition.

---

## Spec A — Mine the maintainers, not the code

**The idea.** Every other approach guesses what this team would merge. Phase 1
showed I guess badly: I predicted `@since` gaps at 1–5% and reality was 100%
adherence. Rather than guess again, read what maintainers actually say in review.

**Deterministic stage.** Fetch closed PRs from `spring-projects/spring-framework`
and extract review comments authored by users with `author_association` of
`MEMBER`/`OWNER` on PRs that were **merged after changes were requested**. That
last filter is the whole trick: it isolates comments that *caused a change*, which
is precisely "a thing the team requires that a contributor did not know."

Discard: bot comments, `LGTM`-class one-liners, comments on the PR body rather
than a diff hunk.

**Reduction.** Unmeasured — needs an authenticated `gh` token to size. Order of
magnitude: a few thousand comments, filtered to a few hundred. One-time cost, not
per-run.

**LLM stage.** Cluster the surviving comments into recurring *rules*, and for each
cluster emit: the rule in one sentence, 3 verbatim example comments with links,
and — critically — **whether the rule is mechanically checkable**.

**Output is not candidates.** It is a ranked list of *proposed miner specs*. The
LLM feeds the deterministic machine rather than replacing it. Anything it proposes
that is already enforced by `src/checkstyle/checkstyle.xml` is dropped
automatically by cross-referencing the ruleset, which Phase 1 showed kills most
plausible-sounding ideas.

**Cost class.** `a_api`, one-off rather than scheduled. This finally gives cost
class (a) a legitimate occupant after stalled PRs were ruled out.

**Why this first.** It is the only proposal that reduces uncertainty about all the
others. It is bounded, cheap, run once, and its output is reviewable by a human in
minutes.

**Failure mode.** Review comments skew toward whatever was contentious, not
whatever is common. Mitigate by requiring ≥5 independent instances before a
cluster becomes a proposed rule.

---

## Spec C — Doc↔code semantic drift

**The idea.** `docs_dead_ref` verifies that a referenced *type exists*. It cannot
verify that surrounding prose still *describes what the code does*. That is
irreducibly a reading task.

**Deterministic stage.** For each `.adoc` page, resolve the Spring types it
references, then compare the page's last-modified commit against those types'
last-modified commits. Flag pages where code moved on and docs did not.

**Reduction — measured, with a caveat.** 470 `.adoc` → **22 pages**. But the
signal is contaminated: all 22 report `doc last = 2025-07-10`, a bulk docs commit.
This is the same failure mode as the PR `updated_at` reset, and it is the second
time a naive recency signal has proved untrustworthy in this repo.

**Required correction before building:**
- ignore commits touching >50 files (bulk reformats, licence-header sweeps)
- compare per-section using `git log -L` on the section's line range, not per-file
- require a *substantive* code change: the referenced type gained or lost a public
  member, not merely any commit

Expect the true set to be well under 22. **Re-measure after the correction; if it
lands near zero, kill this spec** — that outcome is a finding, not a failure.

**LLM stage.** For one flagged section plus the current source of the types it
references: does the prose still describe this code? Output `accept`/`reject` plus
a specific contradiction citing `file:line`. Reject on uncertainty.

**Cost class.** `b_source`.

**Mergeability.** Good if the LLM cites a concrete contradiction; poor if it
produces "this could be clearer." The prompt must demand a factual mismatch and
the deterministic layer should discard any verdict whose rationale contains no
`file:line` citation — a cheap, checkable output constraint.

---

## Spec D — Diagnostics quality

**The idea.** Spring is unusually careful about exception messages. A message that
names the constraint but not the offending value costs every user who hits it a
debugging session.

**Deterministic stage.** Find `throw new <Exception>("string literal")` in
`src/main/java` where the message contains **no interpolation** — no concatenation,
no formatted argument, no reference to a parameter in scope. Then narrow to
throws inside a method that *has* parameters, since a message that could have named
a value is the interesting case.

**Reduction.** Unmeasured. Needs an AST-level pass; regex will not distinguish
concatenation reliably. Java parsing from Python is the main build cost here.
`javaparser` via a small helper, or OpenRewrite, would do it properly — worth
noting that **OpenRewrite is the right tool for any AST-level work** and is
already familiar to this ecosystem, Spring shipping upgrade recipes for it.

**LLM stage.** Given the throw site and its enclosing method: would this message
let a user identify what they did wrong? Propose a replacement naming the offending
value. Reject when the constraint is already unambiguous.

**Cost class.** `b_source` to find, `c_build` to verify — any message change should
compile and pass tests before it is proposed, and some messages are asserted on in
tests, which is itself a useful signal that the message is load-bearing.

**Mergeability.** Moderate and worth honest scepticism. Diagnostics are a matter of
taste, and taste is exactly where this maintainer team is most exacting. Recommend
gating on a stricter rule: only propose where an existing sibling method in the
same class already interpolates the value, making the inconsistency internal to one
file and objectively demonstrable — the same "94% convention" logic that makes
`docs_dead_ref` work.

---

## Ranking

| Spec | Reduction measured? | Build cost | Mergeability | Verdict |
|---|---|---|---|---|
| **A** maintainer review mining | no (needs auth) | low | n/a — produces specs | **build first** |
| **C** doc↔code drift | yes, 470→22, contaminated | medium | good, if citations enforced | build after correcting the filter |
| **D** diagnostics | no | high (needs AST) | moderate | build last, narrowed to intra-class inconsistency |

## What would make me abandon all three

If Spec A's output is dominated by rules already enforced in
`src/checkstyle/checkstyle.xml`, that is strong evidence the remaining
LLM-addressable surface is genuinely small — consistent with Phase 1, where 10 of
13 candidate categories died against existing enforcement. In that case the honest
recommendation is to stop adding miners and let the two deterministic ones run on
a slow schedule as regression detectors.

That outcome is a legitimate result of this work, not a failure of it.
