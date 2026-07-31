# 05 — Specs C and D

Spec `03` pre-committed to kill conditions for both. C survived and was built as a
**harvester**. **D was killed.**

---

## Spec D — diagnostics quality: KILLED

The hypothesis: exception messages that name a constraint but not the offending
value cost every user who hits them a debugging session. The proposed pre-filter
was `throw new X("string literal")` with no interpolation, narrowed to files that
*also* contain interpolating throws — an intra-class inconsistency, the same
"94% convention" logic that makes `docs_dead_ref` work.

Measured across `src/main/java`:

| | |
|---|---|
| literal-only throw messages | 619 |
| interpolating throw messages | 2079 |
| files containing both forms | 179 |
| literal-only messages in those files | **345** |

The narrowing worked arithmetically and failed completely on precision. An unbiased
sample (every 17th hit, all modules):

```
"Property 'mappedObjectName' is required"
"MessageHeaders is immutable"
"Transaction is already completed - do not call commit or rollback more than once"
"Records requires ASM8"
"Illegal operation: connection is closed"
"Specified classes are incompatible with delegates"
"Target method must not be non-static without a target"
```

Every one is a complete message. **The absence of an interpolated value does not
indicate a poor message — it usually indicates a constraint with no value to
report.** Several hits are also in vendored ASM/cglib, which is off-limits anyway.

Three reasons to stop rather than narrow further:

1. Distinguishing "should have named a value" from "there is no value" requires
   understanding the constraint. That is irreducibly a reading task, so the
   pre-filter would hand 345 items to a model for perhaps a handful of proposals.
2. Exception messages are **prose**, and the harvested review corpus shows this team
   rejecting unsolicited prose changes outright — 17% of maintainer comments are
   "this is intentional, please revert", including every grammar correction in
   PR #28426.
3. Any proposal would be a matter of taste, in the area where this team is most
   exacting.

Killed with evidence, per the spec. No code written.

---

## Spec C — doc↔code drift: BUILT, as a harvester

`miner.py harvest --source docs_drift`. Cost class `b_source`.

**Why a harvester and not a miner.** `docs_dead_ref` can *prove* a reference is dead
— the type is absent from the tree. Nothing deterministic can prove a paragraph no
longer describes its subject. The honest deterministic output is "this page
documents classes that gained or lost public API since the page was last
substantively edited": a strong reason to read, not a fix. Emitting that as a scored
candidate would fill the ledger with unverifiable items.

This made harvesters no longer all `a_api`, so they now declare a cost class like
miners do, and `HarvestDefect` moved to a shared `harvesters/base.py`.

### Funnel

```
pages_scanned                    464
pages_with_enough_refs            36     (>=3 referenced framework types)
pages_code_moved_on               20     (bulk commits excluded)
pages_with_signature_change        9     (annotation-only churn cancelled)
bulk_commits_excluded             88
```

**464 → 9.** That 98% reduction is the entire justification for a downstream reading
stage.

### Three filters, each earned

**1. Bulk-commit exclusion.** Using raw last-commit dates flagged 22 pages that all
shared `2025-07-10` — a repo-wide docs sweep. Same trap as the PR `updated_at`
reset; the second time a naive recency signal proved untrustworthy here. Commits
touching >50 files are ignored on both sides. After this the doc dates spread
naturally across 2023–2026.

**2. Public-signature requirement.** Any commit at all is far too weak — internal
refactors do not invalidate prose. Restricting to added/removed `public`/`protected`
declarations took 20 pages to 14.

**3. Annotation-only cancellation — the one that mattered most.** 176 recorded
signature changes were sampled: **126 (71%) were annotation-only**, and **six of the
fourteen pages had nothing else**. Their entire signal was the JSpecify nullability
migration — precisely the mechanical sweep the brief warned was already complete,
turning up as noise in a different guise.

Cancelling `-`/`+` pairs that differ only by `@Nullable`, `@NonNull`, `@Override`
and similar took 14 pages to 9 and **completely reordered the ranking**:
`core/appendix/xml-custom.adoc` fell from rank 1 with 111 changes to rank 4 with 7.
Cancellation is by multiset, so one annotation edit cannot swallow a genuine
repeated change.

### Current output

| sig changes | doc last | code last | page |
|---:|---|---|---|
| 21 | 2024-10-23 | 2026-05-29 | `data-access/orm/jpa.adoc` |
| 11 | 2024-09-02 | 2026-03-23 | `integration/jmx/interface.adoc` |
| 8 | 2023-12-27 | 2025-10-29 | `core/beans/factory-nature.adoc` |
| 7 | 2023-04-20 | 2026-06-27 | `core/appendix/xml-custom.adoc` |
| 6 | 2025-07-10 | 2026-01-27 | `data-access/transaction/strategies.adoc` |
| 2 | 2024-04-08 | 2025-08-06 | `core/aop-api/pfb.adoc` |
| 1 | each | | `targetsource.adoc`, `aot.adoc`, `factory-scopes.adoc` |

### Bug found while testing

`_RE_SIGNATURE` required `public`/`protected` at the start of the line, so a diff
adding `@Override` inline captured the `-` side but not the `+`. The orphaned `-`
line could not cancel and read as drift where nothing had changed. Widened to
tolerate leading annotations. Regression test:
`test_adding_override_is_not_drift`.

Same shape as every other bug in this project: a *plausible-looking* signal produced
by a component that had partly failed.

### Honest limits

- **A flagged page is not a defect.** It is a page worth reading. Precision for "the
  prose is actually wrong" is unknown and not deterministically knowable.
- Ranking by signature-change count is a churn proxy, not a wrongness proxy.
- Pages referencing fewer than 3 framework types are skipped; `files_truncated`
  records where the 40-file pathspec cap bit, rather than hiding it.
