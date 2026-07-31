# 04 — Spec A: maintainer review-comment harvesting

Built: `miner.py harvest --source review_comments`. Deterministic, cached, no LLM.
Cost class `a_api` — this finally gives that class a legitimate occupant after
stalled PRs were ruled out in Phase 1.

## Method

List closed PRs sorted by **`created`**, never `updated` — every open PR in this
repo reports the same `updated_at` after a bulk label event, and the hazard applies
equally to closed ones. Keep merged PRs, then keep only those with a maintainer
`CHANGES_REQUESTED` review.

That last filter is the whole idea: it isolates comments that demonstrably *caused
a change*, which is a rule the team enforces that the contributor did not know.
Frictionless merges teach nothing; rejected PRs are about the idea, not the craft.

Comment filters: `author_association` in `{MEMBER, OWNER}`, not a bot, not the PR
author, ≥40 characters after stripping quoted text and code fences, not in a
trivial stoplist.

## First run — unauthenticated, 4 merged PRs

```
prs_listed                       32
prs_merged                        4
prs_with_changes_requested        1
comments_seen                    13
comments_kept                     4      (all from one maintainer, sbrannen)
```

All four kept comments are **the same rule**, restated with escalating emphasis
across four files in PR #36899:

> Convert `instanceof` and `type.isInstance()` assertions to
> `assertThat(...).isInstanceOf(...)`, **without a custom failure message** —
> AssertJ generates a meaningful message anyway.

The method works. A 4-PR sample surfaced a specific, mechanically checkable rule.

## Then the discipline kicked in

Phase 1's standing lesson is *assume it is already enforced, then verify*. So:

| Question | Answer |
|---|---|
| Enforced by checkstyle? | **No.** No rule in `src/checkstyle/checkstyle.xml` mentions `instanceof`, `isInstance` or `isInstanceOf`. |
| Adherence to the target form? | **1322** uses of `assertThat(...).isInstanceOf(...)` |
| `assertThat(x instanceof Y).isTrue()` | **0** |
| `assertThat(X.isInstance(y)).isTrue()` | **5** |

99.6% adherence, unenforced — precisely the "94% convention" shape the brief
describes.

Then the five were checked individually, and **all five are false positives.**
They are in `spring-core/src/test/.../ResolvableTypeTests.java:1060-1064`, where
the receiver is a `ResolvableType` and `ResolvableType.isInstance(Object)` is a
public method of the class under test (`ResolvableType.java:248`), exercised in the
same block as `isAssignableFrom`. Rewriting them to `assertThat(y).isInstanceOf(…)`
would stop testing the method entirely.

## Full authenticated run — 300 merged PRs

```
prs_listed                     2717
prs_merged                      300
prs_with_changes_requested       12      (4% of merges)
comments_seen                    62
comments_kept                    30
```

30 comments, 3 maintainers (sbrannen 23, bclozel 5, snicoll 2), across 7 PRs.
**No clustering model was needed** — 30 comments are readable directly. Worth
recording as a caution against reaching for an LLM before checking the corpus size.

### Clusters, by reading

| Cluster | n | Minable? |
|---|---|---|
| **"This is intentional — please revert"** | 5 | no, but see below |
| Docs-config hygiene (unused/duplicated antora attributes) | 6 | **yes** |
| AssertJ `isInstanceOf` conversion | 4 | rule real, inventory 0 |
| Naming and design dialogue | 6 | no |
| Correctness/security review (log forging, URI encoding) | 5 | no |
| Misc / self-corrections | 4 | no |

### The most important finding is a warning, not an opportunity

**5 of 30 comments (17%) are "this is intentional, please revert."** In PR #28426 a
contributor made English-grammar corrections to javadoc — "a"/"an", a comma — and
the maintainer rejected every one:

> *"When read as 'at Bean', the use of 'an' is correct. Thus, please revert this change."*
> *"The prose here uses verbal phrases... does not need a comma. So please revert this change."*

In PR #35163, two more reverts for adding assertions a helper already made.

This is precisely the class of PR an automated convention-miner would generate, and
it is rejected on sight. It is the strongest available evidence for the
precision-first stance, and an argument against ever shipping a miner that proposes
stylistic "improvements" to prose. Encode it as a **negative** rule: apparent
deviations in javadoc wording are presumed deliberate.

### The actionable rule: unused Antora attributes

From PR #31619, sbrannen auditing `framework-docs/antora.yml`:

> *"A quick search reveals that we're only using that base URL for a single `SPR-`
> issue... I suggest we remove the `issues-old` attribute."*
> *"This appears to be overridden in `attributes.adoc`. Do we need both?"*

He then removed the obsolete sections himself. That is **documented precedent** that
this team accepts this cleanup — not an inference about what they might accept.

Measured against the current tree: **72 attributes in the `asciidoc.attributes`
block, 5 defined and never referenced.**

```
hibernate-validator-site     jackson-docs        kotlin-issues
spring-boot-issues           spring-framework-reference
```

First measurement returned 14, contaminated in the by-now-familiar way: the naive
extractor grabbed 4-space-indented keys from anywhere in the YAML, catching the
`ext:` block (`run`, `scan`), Asciidoctor built-in settings (`chomp`, `fold`,
`table-stripes`, `attribute-missing`), and attributes consumed by the `include-code`
extension rather than by `{}` substitution (`include-java`, `include-kotlin`,
`include-xml`). Scoping to the attributes block and denylisting those leaves 5.

Proposed as `docs_unused_attribute` — same dead-config family as
`config_dead_entry`, and it should probably live in that miner rather than a new one.

## Verdict on the AssertJ rule

Rule discovered ✓ · rule genuine ✓ · rule unenforced ✓ · **current inventory 0.**

Consistent with everything Phase 1 found. This repo's residue is nearly always
either already machine-enforced or deliberate, and the deliberate cases are usually
the system under test.

**This does not make the rule worthless — it makes it a regression detector.**
Encoding it catches the *next* contributor who writes the deviating form, which is
the steady-state value argued for earlier: the tool's job here is catching new
drift within days, not clearing a backlog that does not exist.

Whether to build it is a judgement call: a miner with zero current candidates is
hard to justify on its own, but nearly free if it rides alongside another
test-source scan.

## What this says about Spec A itself

The harvester is worth keeping regardless of this particular rule's yield, because
it converts guessing into measurement. Recommended next step: authenticate and run
at `--limit 300`. One PR produced one rule; a few hundred should produce a ranked
set, and the ones worth encoding will be those whose deviating form is *not* the
system under test.

The clustering step over the corpus remains downstream and outside this tool. Note
that on this sample no clustering was needed — four comments, one rule, visible by
reading. That is itself a caution against reaching for a model too early.

## Bugs fixed during the build

### Token discovery failed silently against an older `gh`

The installed CLI is `gh 2.4.0` (Debian/Ubuntu shipped it for years). `gh auth
token` does not exist there — it was added around 2.17. The failure was quiet in a
nasty way: the unknown subcommand prints its **usage text to stdout** and exits 1,
so any implementation trusting stdout would have used 141 characters of help text
as a bearer token.

The original code checked `returncode == 0`, so it correctly returned `None` — but
that meant an authenticated user still appeared unauthenticated, with `core: 54/60`
proving the token was unused. Fixed with a fallback chain: environment variables,
then `gh auth token`, then `gh config get -h github.com oauth_token` (which works
on 2.4). Plausibility check added on the result: single line, ≥20 characters.

Recorded because it is the same shape as the other bugs in this project — a
*plausible-looking* value produced by a component that had actually failed. Return
codes, not output, are the trustworthy signal. Regression test:
`test_usage_text_is_never_mistaken_for_a_token`.

### `rstrip` with a character set

`_is_substantive` used `rstrip(".!softly ")` to trim punctuation. Character-set
`rstrip` strips *any* of those characters, so `"okay"` became `"oka"` and missed the
trivial-comment stoplist. Narrowed to `rstrip(" .!")`. Regression-tested in
`test_rejects_trivial_bodies`.
