# 01 — Phase 1 reconnaissance

All figures below come from reading the checkout at `1d1aac3674` or querying the
GitHub API. Nothing is asserted from prior knowledge of Spring.

## Expectations vs reality

Scored against `00-expectations.md`, which was written first and has not been edited.

| Category | Expected | Reality | |
|---|---|---|---|
| Unresolved `@link`/`@see` | Low; probable dead end | 4 dangling in ~23,000 (0.015%) | ✅ |
| Missing `@since` on new API | **Real, 1–5% of new types** | **100% adherence** on every type added to a `spring-*` module since 2024 | ❌ badly wrong |
| Internal `@Deprecated(forRemoval)` use | Small but real, <20 | 309 suppressions, **0 removable** | ❌ wrong |
| `@Disabled` w/o issue link | 20–60, half unexplained | 185 tokens → ~5 actionable | ⚠️ 76% noise |
| `Thread.sleep` in tests | 100+, mostly legitimate | 176; ~35% convertible | ✅ |
| TODO/FIXME | 200–500, mostly permanent | 143 total; ~8 actionable | ✅ nature, 3× over on volume |
| Doc snippets vs signatures | Real but expensive; many `include::`d | Real, **cheap**, and the `include-code::` split is the fault line | ⚠️ right for the wrong reason |
| Stalled PRs | Real; mergeability is the hard part | Discovery signal destroyed; mergeability worse than expected | ❌ wrong |

Additional hypotheses — package-info presence, AssertJ adoption, `*Tests` naming,
copyright-year drift — **all wrong**, every one already machine-enforced.

**The systematic error**: I underestimated how much this repo enforces. My prior was
calibrated on ordinary Java codebases. That miscalibration is the most useful thing
Phase 1 produced, because it inverts the search: stop looking for unenforced
conventions, start looking for *places the enforcement does not reach*.

## Repo shape

`7.1.0-SNAPSHOT`, branch `main`. 24 modules. ~845k LOC main Java, ~631k test.
5212 main files, 3760 test+testFixtures, 390 Kotlin, 470 `.adoc`.
Toolchain JDK 25, bytecode `--release 17`, Gradle 9.6.1.
PRs run `./gradlew check antora` on JDK 25 only. DCO required.

`CONTRIBUTING.md:68` — *"Should you create an issue first? No, just create the pull
request."* `:127-130` — docs are Asciidoctor and trivial doc changes may be edited
straight from GitHub. Both lower friction for the surviving category.

## Already enforced — off the table

`ConventionsPlugin` applies Checkstyle, ArchUnit, javac lint, Kotlin conventions and
test conventions to all non-`framework-*` projects. Checkstyle
(`src/checkstyle/checkstyle.xml`, 287 lines, v13.7.0) alone kills:

- non-AssertJ assertion APIs, banned outright (`:254-275`)
- `package-info.java` presence (`:18`)
- `*Test` vs `*Tests` naming (`:57-61`)
- `@since` **format** (`:211-218`) — but *not* its presence
- `@author` presence (`:148-150`)
- `public` `@Test` methods (`:283` `SpringJUnit5Check`)
- `printStackTrace` / `System.out` (`:226-239`)
- JUnit 4 imports, `@Test(expected=)` (`:126-130`, `:247-253`)
- non-JSpecify nullability imports (`:115-120`)
- star/unused imports, import order, tabs, trailing whitespace
- copyright header, pattern `20\d\d-present`

Verified absent: JaCoCo/coverage gate, PMD, SpotBugs, japicmp/revapi, standalone
Error Prone, Spotless, Sonar, forbidden-apis, CodeQL, Dependabot, CODEOWNERS, git
hooks, spring-javaformat *formatter* task, `@since` presence check, commit-message
linting.

## The central finding: the enforcement shadow

Three independent methods converged on the same boundary.

| Zone | Files | Enforcement | Drift |
|---|---|---|---|
| `spring-*/src/main/java` | 5212 | full | `@since` 100%, javadoc refs 99.985%, package-info 100% |
| `src/test` + `testFixtures` | 3760 | javadoc checks suppressed (`checkstyle-suppressions.xml:12`), no `-Werror` | all 4 broken FQN javadoc refs |
| `framework-docs` java/kotlin | 413 | none (`build.gradle:14`) | 174 of 180 missing-`@since` files |
| `framework-docs/**/*.adoc` | 470 | none | highest |

Reinforcing: per-module javadoc sets `Werror = false` (`gradle/spring-module.gradle:86`),
and the strict aggregate javadoc (`framework-api.gradle:47`) never runs on PRs.

## Surviving category: dead API references in `.adoc`

216 `include-code::` directives bind to compiled sources and cannot drift.
~1169 inline `[source,java]` blocks compile against nothing.

| Extractor | Distinct refs | Unresolved | Precision after denylist |
|---|---|---|---|
| XML `class="org.springframework…"` | 80 | 14 | 7/7 = 100% |
| `{spring-framework-api}/…html` | 227 | 1 | 1/1 = 100% |
| Bare FQN prose | 258 | 42 | ~6–10 real |

Confirmed, each traced to a deletion commit:

1. `core/beans/factory-extension.adoc:463` — documents
   `org.springframework.beans.factory.config.PropertySourcesPlaceholderConfigurer`;
   the class has lived in `context.support` since **2011** (`b3ff9be78f`).
   Copy-pasting the documented XML throws `ClassNotFoundException`.
2. `data-access/oxm.adoc:441-465` — a whole section for `JibxMarshaller`;
   **JiBX removed 2021-09-17** (`3c8724ba3d`). `src/nohttp/allowlist.lines:2` still
   whitelists `http://jibx.sourceforge.net`, so the enforcement tool was configured
   to permit the dead link.
3. `integration/appendix.adoc:176,204` — `org.springframework.ejb.access.*`, removed
   in the 2021 Jakarta EE 9 migration (`d84ca2ba90`).
4. `integration/cache/annotations.adoc:74` — `DefaultKeyGenerator`, removed **2016**
   (`b5db5d3aac`). Prose reference, so only found with `--include-experimental`.
5. `core/beans/context-introduction.adoc:1036,1047` — javadoc links to
   `SpringContextResourceAdapter`, removed 2021. Reader-facing 404s.

## Categories killed

| Category | Raw | Real | Why it dies |
|---|---|---|---|
| Assertion style | ~230 | **0** | AssertJ 57,292 to ~4; `assertThrows` population is 2, both Kotlin where conversion is a regression |
| Test naming | 19 | **0** | 2,815 `*Tests` to 1 `*Test`, and that one is an annotation declaration |
| `public` `@Test` | 112 | **~1** | 109/112 are JUnit 4, where the runner *requires* `public` |
| `@since` presence | 296 | **0** | 100% on types added to real modules since 2024 |
| Class javadoc | 249 | **0** | all cglib/asm/framework-docs/buildSrc |
| `package-info` | 141 | **0** | all buildSrc + framework-docs, explicitly suppressed |
| Copyright drift | — | **0** | structurally impossible; headers are `2002-present` |
| Javadoc `@link` | 27k | **4** | doclint `Werror=true` gates the aggregate build |
| `@Deprecated` call sites | 309 | **0** | every one is compat plumbing or a JDK/third-party deprecation |
| `XXX` / `HACK` | 21 / 0 | **0** | all 21 `XXX` are test *data* strings |

Surviving as single-PR items, not campaigns:
- **`Thread.sleep`** — Awaitility is declared only in `spring-context` and
  `spring-test`, so anywhere else this becomes a "new test dependency" PR. Best
  target is ~13 sites in `EnableSchedulingTests.java`, excluding `:117-122`,
  `:130`, `:253-294` and in-task-body sleeps, where converting breaks the assertion.
- **`@Disabled`** — 185 tokens collapse to ~5 bare sites. `@DisabledInAotMode`,
  `@DisabledIf` and `@Ignore` are 141 of 185 and pure noise.

## Stalled PRs — damaged

140 open PRs, 124 (89%) `status: waiting-for-triage`,
`ideal-for-contribution` open issues = **0** (independently confirms the premise).

**`updated_at` is unusable**: every open PR reports `updated_at = 2026-05-01`, a mass
bot/label event that flattened the queue. "Untouched >180d" returns 0 despite PRs
created 2025-05 with zero comments. Staleness must be rebuilt from `created_at` +
comment count + commit dates.

The deeper problem is mergeability. These are other contributors' PRs under DCO. A
junior cannot rebase someone else's branch — only open a competing PR into a queue
already 140 deep and triage-starved, which adds to the exact bottleneck the premise
identifies.
