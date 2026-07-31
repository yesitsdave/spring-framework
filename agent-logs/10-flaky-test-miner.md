# 10 — CI failures: the `ci_failures` harvester and `flaky_test` miner

The brief's "improving the speed and quality of the CI" bullet had no coverage.
This step adds it, using the layering the tool already has: a deterministic API
harvester builds a corpus, and a miner that is a pure function of that corpus
plus the working tree emits candidates.

## Why flakiness is the right CI angle for this repo

`TestConventions.java:100-101` applies the Gradle test-retry plugin with
`maxRetries = 3` on CI and **`failOnPassedAfterRetry = true`**. Two
consequences, both load-bearing:

1. The team already treats flakiness as a first-class concern — the
   infrastructure to catch it is deliberate.
2. A flaky test **fails the build even when its retry passes**, so on main,
   flakiness is visible as *failed runs in an otherwise green series*. No
   Develocity access needed, no artifacts needed (this CI uploads none).

Main-branch workflows only (`ci.yml` nightly, `build-and-deploy-snapshot.yml`
per-push). PR-branch failures are usually caused by the PR, which destroys
attribution — the same reasoning that killed the stalled-PR category in recon.

## Recon results that shaped the design

Manual classification of every reachable failed run before writing any code:

- Job console logs DO name failing tests (`Class > Nested > method() FAILED`
  plus `Cause at File.java:NNN`) — my first grep missed them because I guessed
  the format instead of reading a real log. Symbol-level identity is available.
- Log retention is ~90 days; older failures are dated husks. Recorded per
  record as `log_available: false`, never hidden.
- The failure taxonomy is *not* mostly flakes: of 19 harvested failures, 8 are
  test failures, 4 javadoc, 2 antora, 1 nohttp, 4 unknown (logs expired).
  The non-test failures are kept and classified — see "the javadoc gap" below.
- Develocity build scans (`ge.spring.io/s/...`) appear in every log. The API
  behind them is auth-walled, but the *link* is extractable and goes into each
  candidate's evidence — one click from candidate to full failure detail.

## Precision gates in the miner

- Runs classified `test` only, and only runs where ≤10 tests failed — a
  mass failure is a bad commit, not a flake.
- **Two or more distinct runs** must contain the same test. Singletons stay in
  the corpus (`corpus show --source ci_failures`), never in the ledger.
- The test class must resolve to exactly one file under a test source root in
  the working tree; deleted/renamed/ambiguous classes emit nothing. The
  project's founding rule — a candidate that cannot be traced to a line in the
  working tree does not exist — applied unchanged.
- Parameterised invocations (`[1]`, `[2]`, …) collapse into one identity;
  fingerprints exclude line numbers, so both re-runs and line drift dedupe.
- Sanity ceiling: more than 12 distinct "flaky" tests from a ~90-day window on
  a repo whose main is >90% green means the filters broke; the miner raises a
  defect instead of emitting.

## Live results (window 2026-04-21 → 2026-07-30, 200+ runs examined)

Two candidates, both independently confirmed by the manual recon done *before*
the code was written:

```
[100] SimpleAsyncTaskExecutorTests.taskTerminationTimeoutWithImmediateCancel
      spring-core/.../SimpleAsyncTaskExecutorTests.java:167  AssertionError
      3 failures: 2026-05-11 (java21Test), 2026-06-17 (java24Test), 2026-06-25 (test)
      Failing across three JDK task variants -- it is the test, not a JDK.

[ 65] RetryTemplateTests.TimeoutTests.retryableWithTimeoutExceededAfterSecondRetry
      spring-core/.../RetryTemplateTests.java:802  AssertionError
      2 failures: 2026-06-08, 2026-06-15 (java21Test both)
```

Both are timing-assertion tests; each occurrence was 1 failure among ~5000
passes with the next run green. Singletons correctly left in the corpus:
`AsyncExecutionTests.asyncClassListener` (Awaitility timeout),
`RestClientProxyRegistryIntegrationTests.basic` (BindException — a port-binding
test, the exact fragility pattern a maintainer rejected in review PR #1612),
`MockMvcTesterIntegrationTests` (async wait). If any recurs, the next harvest
promotes it automatically.

Why these are unusually junior-safe for this repo: the presumed-deliberate
hazard does not apply. Maintainers themselves demand robust tests (#1612:
"please make the test robust"), and the evidence pack — dates, run URLs, scan
links, cause and line — is exactly the "bring evidence" bar the review corpus
established.

## The javadoc gap — a CI-quality finding for the final report

4 of 19 harvested failures are javadoc breakage reaching main
(`:framework-api:javadoc` ×2 on 2026-07-08/07-22, `:spring-context:javadoc`,
plus dokka noise). Recon (§01) established *why*: PR CI runs `./gradlew check
antora` and nothing makes `check` depend on the aggregate javadoc, so javadoc
breakage merges cleanly and then fails the *deploy* pipeline. The corpus now
provides live incident evidence for what was previously an inference from build
files. Recommendation (for the maintainers, in the final report): a cheap
javadoc task in the PR build would move these 4 failures from post-merge to
pre-merge. Not junior work — a one-line CI recommendation with incidents
attached.

## Implementation notes

- `github.py` grew `get_text` for job logs: redirects to signed blob storage
  are followed manually with credentials dropped at the boundary (the blob host
  rejects requests still carrying the GitHub Authorization header), and expired
  logs (404/410) cache a "gone" sentinel so re-runs are byte-stable.
- The miner declares cost class `a_api` by provenance: it runs offline, but its
  input exists only after an API harvest.
- Determinism verified live: back-to-back miner runs produce byte-identical
  candidate files; the harvester is cache-backed and sorted.
- Building this exposed **bug #8** (see `02-decisions.md`): `docs_drift`'s
  `--since` boundary drifted with the time of day the harvest ran. Found
  because two fixture tests failed before noon and passed after it; fixed by
  switching to the `doc_commit..HEAD` graph range. The real corpus was
  unaffected (same 9 pages, same order).

209 tests, all passing. Ledger now tracks 18 candidates across 4 sources.
