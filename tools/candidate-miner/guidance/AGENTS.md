# Contributing to Spring Framework — guidance for coding agents and new contributors

> **Status: DRAFT RECOMMENDATION — not a Spring Framework file.**
> This is a proposed root-level `AGENTS.md` for the repository, drafted as part of
> a contribution-mining project. It has not been reviewed or adopted by the Spring
> team. Every rule below is traced to its source: a maintainer review comment
> (cited by PR number), a measurement of the working tree at commit `1d1aac3674`,
> or a file in the repository (cited by path and line). Rules are not stated from
> general knowledge of Spring etiquette.
>
> **Verifying the citations**: PR-number citations can be checked directly on
> GitHub, which is the durable record. The harvested review-comment corpus they
> were mined from (`out/review_comments.corpus.jsonl`) is derived output and is
> *not* committed — regenerate it with `./miner.py harvest --source
> review_comments` (needs a GitHub token). Working-tree measurements are
> reproducible only at the cited commit.
>
> **Known bias**: the review-comment corpus behind this document is 61 comments
> across 23 merged PRs (2017–2026), 54 of them from one maintainer. Treat the
> style rules as one senior maintainer's consistently-applied lens, not a
> team-ratified standard.

## The one rule that outranks all others

**Presume existing code is deliberate.** The most common maintainer response to
unsolicited "improvements" — grammar fixes, removal of redundant-looking checks,
tidying obsolete-looking documentation, structural refactors — is *"this is
intentional, please revert"* (PRs #1943, #23478, #24789, #24933, #25448, #28426,
#35163). Documented examples of things that looked wrong and were not:

- A utility class that is deliberately *not* abstract, explained in its
  constructor's Javadoc (#1943).
- A "redundant" annotation check that actually checks a different object than
  the neighbouring call (#24933).
- A lowercase constant "typo" that was intentional, to point out that case
  mattered (#24789).
- The article "an" before `@Bean` — correct when read aloud as "at Bean" (#28426).
- A change that was *"technically equivalent"* but was rejected because *"it
  breaks with the structure of the rest of the method"* (#23478). Local
  consistency beats local improvement.

The bar for changing working code is **evidence, not plausibility**. In the same
PR where a refactor was initially refused, the maintainer accepted it once the
contributor supplied benchmark references: *"I had momentarily forgotten about
those findings from Shipilёv. Thanks for the links. We'll keep the proposed
change!"* (#23478). Put your evidence in the PR body — measurements, links,
reproduction steps — not in a follow-up comment after a maintainer objects.

## Hard prohibition: vendored third-party code

Never modify classes under `org.springframework.asm`, `org.springframework.cglib`,
or `org.springframework.objenesis`. These are repackaged forks of ASM, CGLIB, and
Objenesis; the team's written policy, quoted verbatim in four separate PRs
(#1943, #23478, #24933, #25450):

> *"Please refrain from modifying classes under `org.springframework.asm`,
> `org.springframework.cglib`, and `org.springframework.objenesis`. Those include
> repackaged forks of the third-party libraries ASM, CGLIB, and Objenesis. Any
> refactoring to those classes should take place upstream in the originating
> projects."*

This matters more than it looks: these packages contain most of the repo's
visible style deviations (e.g. 34 of the 34 `catch (… e)` sites that violate the
`ex` naming convention below), so naive linting will steer you directly into
policy-protected code.

## Scope a PR to one concern

Drive-by fixes get individually reverted even when correct in isolation: obsolete
documentation encountered mid-PR *"needs to be reworked separately"* (#25448,
three times in one PR). Conversely, when a maintainer asks for a pattern change,
apply it to **every** occurrence in your PR, not only the flagged line — *"please
modify **all** affected assertions in this PR"* (#36899, escalated across three
comments).

## Style rules maintainers actively enforce

Checkstyle (`src/checkstyle/checkstyle.xml`) already enforces a great deal —
assertion library (AssertJ only), `*Tests` naming, imports, headers, `@since`
format. Run it before pushing; anything it catches will not reach human review.
The rules below are the ones enforced *by reviewers*, beyond the tooling:

- **Name caught exceptions `ex`, not `e`** (#1784). Measured at commit
  `1d1aac3674`: 2192 `catch (… ex)` vs 34 `catch (… e)` in Spring-authored main
  code — 98.5% adherence, and all 34 exceptions are in the vendored packages
  above.
- **Do not mark local variables `final`** (#24683). *Caveat: measured adherence
  is weak (713 `final` locals exist in main code), so treat this as reviewer
  preference for new code, not something to "fix" in existing code.*
- **Do not store a value in a local variable used only once** (#24683).
- **Do not guard constant-string log statements with `isDebugEnabled()`** — the
  concatenation of literals is folded by the compiler (#24683).
- **Avoid needless allocation**: `Collections.addAll(target, array)` over
  constructing an intermediate list (#24555); a single `Arrays.asList(…)` over
  building a list to copy it (#24586); don't mix a `StringBuilder` with trailing
  `+` concatenations (#36641).
- **Minimal visibility**: utility classes package-private unless there is a
  caller that needs more (#26107); skip defensive assertions in package-private
  classes whose only caller is known (#24796).
- **Javadoc lists**: enclose items in `<ul></ul>` and close each `<li>` (#22777).
  *Caveat: 72% of existing `<li>` tags in the tree have no same-line close — this
  is a bar for new code, not a cleanup mandate.*
- **No redundant `@return` clauses**: factory methods like `create()` omit
  `@return` when it would only restate the obvious; match the precedent in
  `JdbcClient`, `RestClient` (#35163).

## Test-writing rules

- **AssertJ, in idiomatic form.** For "does not throw":
  `assertThatCode(() -> …).doesNotThrowAnyException()` (#25239). For type checks:
  `assertThat(obj).isInstanceOf(X.class)` — with **no custom failure message**;
  AssertJ's generated message is considered sufficient (#36899).
- **No environment-fragile tests.** A test that fails when a fixed port is
  occupied *"is guaranteed to fail… please make the test robust"* (#1612).
- **Test method names describe the scenario precisely**, even at the cost of
  length — a rename to `findAvailableTcpPortWithMinPortEqualToMaxPort()` was
  requested in review (#1612).
- **Assert the behaviour, not just the exception type** — asserting
  `IllegalArgumentException` is insufficient when the constructor throws the
  same type for invalid input (#1784).
- **Think about log output as attack surface**: prefer encoded/raw forms when
  logging URIs, to stay consistent with `URI#toString()` and avoid log-injection
  vectors (#36641).

## Documentation PRs

Documentation is the most welcoming entry point, by the project's own rules:

- No issue needed first — *"No, just create the pull request"*
  (`CONTRIBUTING.md:68`).
- Reference docs are Asciidoctor under `framework-docs/`; trivial fixes can be
  edited directly from GitHub (`CONTRIBUTING.md:127-135`).
- Maintainers engage substantively and collaboratively on docs PRs — including
  correcting their own review comments (#36600, #26852). Precision in technical
  claims is expected: "JVM system property" vs "Spring property" vs "environment
  variable" distinctions were each review points (#36600).

## Mechanics

- **Verification**: PR CI runs `./gradlew check antora` on JDK 25
  (`.github/actions/build/action.yml:49`). If those pass locally, CI will pass.
- **DCO, not CLA**: commits must be signed off (`git commit -s`);
  see `CONTRIBUTING.md:87`.
- **Backports are not your job**: maintainers handle branch targeting; open PRs
  against `main` unless told otherwise (`CONTRIBUTING.md`).

## For coding agents specifically

1. Before proposing any change, check the path against the vendored-package
   prohibition. A lint finding inside `org.springframework.{asm,cglib,objenesis}`
   is not actionable — ever.
2. Never propose a change whose only justification is "this looks
   wrong/redundant/outdated". Either attach evidence (a measurement, a failing
   reproduction, a spec citation) or do not propose it.
3. Keep the diff to the single stated concern; list anything else you noticed in
   the PR description as observations, not changes.
4. When a reviewer requests a pattern change, sweep the entire PR for other
   instances of the pattern before pushing the fix.
5. Match the surrounding code's structure even when an alternative is
   equivalent or marginally better.
