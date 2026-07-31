# Final report — junior-contributor engineering-excellence program for Spring Framework

**Deliverable 1: a deterministic candidate-mining tool (`tools/candidate-miner/`),
its evidence trail (`agent-logs/`), and 18 open, evidence-backed candidates.**

## The thesis

Spring Framework's scarce resource is not work — it is **maintainer review
attention**. The `status: ideal-for-contribution` label has zero open issues,
140 open PRs sit 89% untriaged, and 287 lines of Checkstyle plus ArchUnit,
NullAway and `-Werror` already enforce most things a linter could suggest. The
viable niche for junior contributions is **work where discovery cost exceeds fix
cost**, found in the *enforcement shadow*: docs, test fixtures, and CI
telemetry, where no tool looks (`build.gradle:14` excludes `framework-*` from
all conventions). Precision beats recall throughout — every candidate below is
traced to a line in the working tree or a logged CI incident, and a permanent
human veto (ledger decline) survives every re-run.

## The candidate inventory (18 open)

### Dead API references in reference docs — `docs_dead_ref` (8)

Docs-only fixes, no issue required (`CONTRIBUTING.md:68`), approvable in
minutes. Score reflects extractor precision, relocation certainty, staleness.

| Score | Reference | Where | Fix |
|--:|---|---|---|
| 85 | `beans.factory.config.PropertySourcesPlaceholderConfigurer` | `core/beans/factory-extension.adoc:463` | one-token package fix — class moved to `context.support` in **2011**; the documented XML throws `ClassNotFoundException` |
| 74 | `beans.TestBean` | `core/beans/child-bean-definitions.adoc:22` | editorial: illustrative type never published |
| 74 | `beans.TestBean` | `core/appendix/xsd-schemas.adoc:195` | editorial: same |
| 55 | `jmx.AnnotationTestBean` | `integration/jmx/interface.adoc:153` | unique relocation to `jmx.export.annotation` |
| 54 | `oxm.jibx.JibxMarshaller` | `data-access/oxm.adoc:451` | remove section — JiBX support deleted 2021 |
| 54 | `ejb.access.SimpleRemoteStatelessSessionProxyFactoryBean` | `integration/appendix.adoc:238` | remove — EJB access deleted in Jakarta migration |
| 54 | `ejb.access.LocalStatelessSessionProxyFactoryBean` | `integration/appendix.adoc:176` | remove — same |
| 49 | `jca.context.SpringContextResourceAdapter` | `core/beans/context-introduction.adoc:1036` | fix two 404ing javadoc links — type deleted 2021 |

*(1 further candidate, `DerivedTestBean`, was **declined** in the ledger as the
worked example of permanent suppression.)*

### Dead build-config entries — `config_dead_entry` (7)

Each verified against a live control; each a one-line removal.

| Score | Entry | Where |
|--:|---|---|
| 77 | suppression `Target_BytecodeProviderInitiator` | `src/checkstyle/checkstyle-suppressions.xml:81` |
| 75 | suppression `Target_ClassFinder` | `src/checkstyle/checkstyle-suppressions.xml:46` |
| 75 | antora attribute `spring-framework-reference` | `framework-docs/antora.yml:42` |
| 75 | antora attribute `spring-boot-issues` | `framework-docs/antora.yml:47` |
| 75 | antora attribute `hibernate-validator-site` | `framework-docs/antora.yml:64` |
| 75 | antora attribute `jackson-docs` | `framework-docs/antora.yml:65` |
| 75 | antora attribute `kotlin-issues` | `framework-docs/antora.yml:75` |

Precedent: a maintainer performed exactly this cleanup himself in PR #31619.

### Flaky tests on green main — `flaky_test` (2)

Mined from CI history (Apr–Jul 2026, 200+ runs). Each failed as ~1 test in
~5000 with the next run green; evidence includes dates, run URLs and Develocity
scan links. Junior-safe: maintainers themselves demand robust tests (PR #1612).

| Score | Test | Signal |
|--:|---|---|
| 100 | `SimpleAsyncTaskExecutorTests.taskTerminationTimeoutWithImmediateCancel` (`spring-core:167`) | 3 failures across **three JDK variants** (java21Test, java24Test, test) — it is the test, not a JDK |
| 65 | `RetryTemplateTests.TimeoutTests.retryableWithTimeoutExceededAfterSecondRetry` (`spring-core:802`) | 2 failures in one week, java21Test both |

### Adjudicated documentation drift — `docs_drift` → triage (1)

Found by the LLM triage pathway (deterministic evidence packets, model verdicts
validated against the packet before reaching the ledger — the model cannot
author identities or cite what it was not shown).

| Score | Finding | Where |
|--:|---|---|
| 75 | `LifecycleProcessor` — page says "adds **two** other methods"; interface now declares four (`onPause`/`onRestart` added for 7.0) | `core/beans/factory-nature.adoc:431` |

## What was deliberately *not* proposed

Ten of thirteen initially hypothesised categories were killed by measurement
(details: `01`, `02`, `05`, `08`): assertion style, test naming, `@since` gaps
(predicted 1–5%, measured **100% adherence**), package-info, copyright drift,
javadoc `@link`s, deprecated call sites — all already machine-enforced or
clean. The review-comment corpus added mined *prohibitions*: never touch
vendored `asm`/`cglib`/`objenesis` (written policy, 4 PRs), presume existing
code deliberate, bring evidence. The `ex`-not-`e` convention showed why both
halves matter: 98.5% adherence, 34 offenders — **all 34 in policy-protected
vendored code**. Actionable inventory: zero.

## Recommendations to the maintainers

1. **Run a javadoc task on PR builds.** PR CI is `check antora`; nothing builds
   javadoc, so breakage merges and fails the deploy pipeline instead — 4 logged
   incidents in the harvest window (2 × `:framework-api:javadoc`,
   `:spring-context:javadoc`, + antora). One CI line moves these pre-merge.
2. **Adopt (and edit) the drafted `AGENTS.md`**
   (`tools/candidate-miner/guidance/AGENTS.md`) — contributor/agent guidance
   with every rule cited to a maintainer review comment or a measurement.
3. **Feed the ledger to `status: ideal-for-contribution`.** The 18 candidates
   above are pre-verified issue material; the ledger state machine
   (`new → queued → done`, `declined` = permanent) is built for exactly that
   pipeline.

## Mechanics

- `./miner.py run --miner <name>` — deterministic; byte-identical on the same
  commit; 209 tests including a subprocess test proving `miner.py` cannot reach
  an LLM. `./triage.py run --source docs_drift` — the model pathway, ~$0.26/run.
- Declines are permanent: `./miner.py ledger decline <fp> --reason "..."`.
- Nothing in the Spring tree was modified; the tool is invisible to Gradle.
- **Next (Deliverable 2):** gh-aw workflows scheduling the miners by cost class
  (`a_api` nightly, `b_source` on push, triage weekly), opening issues from
  `queued` candidates with their evidence packs in the body.
