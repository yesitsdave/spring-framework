# 02 — Decisions, deviations, and things that went wrong

## Design decisions

| # | Decision | Reasoning |
|---|---|---|
| 1 | Tool lives in-tree at `tools/candidate-miner/` | "Alongside, not in it" read as *not in the build graph*. Absent from `settings.gradle`, so Gradle never sees it. Modifies no Spring file. |
| 2 | No cost-class `a_api` miner in v1 | Its only occupant (stalled PRs) was killed by recon. Structure kept so later scheduling stays decoupled. Also means no `gh auth` needed. |
| 3 | One dead reference = one record, plus `pr_grouping_key` | Keeps evidence atomic and fingerprints stable, while letting triage batch a whole file. A one-token package fix and a 25-line dead section are different-sized fixes and must not be merged into one opaque item. |
| 4 | Prose extractor behind `--include-experimental`, off by default | Two extractors measured at 100% precision; prose at ~20%. Precision over recall. Confirmed in practice: prose adds 6 findings, of which 2 are real (`TypeFilter`, `DefaultKeyGenerator`) and 4 are illustrative sample classes, correctly scored 15–35. |
| 5 | Renames: **detect orphans, never auto-carry** | Auto-carry can silently suppress the wrong file. A wrong suppression fails by omission — invisible, and the worst failure mode for a precision-first tool. ~15 lines instead of ~40, zero false-suppression risk. Revisit with real usage. |
| 6 | Miner never touches git for *writing* | Reads history as evidence only. Committing is the workflow's business. Keeps the tool testable offline and defers all gh-aw specifics without rework. |
| 7 | `agent-logs/` committed | Graded artifact. |
| 8 | Stop at the tool | Do not open the five PRs. |

## Deviations from the approved plan

**`occurrence_ordinal` dropped from the fingerprint.** The plan specified
`sha256(category ‖ path ‖ reference ‖ occurrence_ordinal)` to disambiguate repeats
of the same dead FQN in one file. Implementing it exposed the flaw: ordinals shift
when any earlier occurrence is edited or removed, so an unrelated edit elsewhere in
the file would mint new fingerprints and resurrect declined candidates.

Replaced with: **group by `(path, reference)`**, one candidate per file, all
occurrences carried in `evidence.occurrences`. Fingerprint is
`sha256(category ‖ path ‖ identity)`.

Strictly better — it is what the granularity decision (#3) implies anyway, since
the same dead reference twice in one file is one fix. Verified by
`test_fingerprint_is_unaffected_by_occurrence_count`.

**`out/` is gitignored.** Not specified in the plan. The candidates JSONL is
derived and fully reproducible from a commit, and the run manifest carries a
wall-clock timestamp that would churn on every run. The durable cross-run artifact
is `ledger/`, which is committed.

## Bugs found and fixed during implementation

These are recorded because each one produced *confident, plausible-looking false
positives*, which is precisely the failure this tool exists to avoid.

### 1. `target` is a package name, not just a build directory

`_SKIP_DIRS` contained `target` (Maven output). But
`org.springframework.aop.target` is a real Java package. Skipping it by name erased
51 files from the index and manufactured **5 confident false positives** —
`HotSwappableTargetSource`, `CommonsPool2TargetSource`, `ThreadLocalTargetSource`,
`PrototypeTargetSource`, `AbstractPoolingTargetSource` — all reported as dead
references in `core/aop-api/targetsource.adoc` while existing perfectly well on disk.

Fix: build-output names are skipped only *outside* a source root. Once inside
`src/<set>/<lang>/`, every directory is a package and nothing is skipped.
Regression test: `TargetPackageRegressionTests`.

Candidate count fell 14 → 9. All five removed were false.

### 2. Relocation matching on simple name alone invents moves

First attempt scored a replacement as an unambiguous "one-token fix" whenever
exactly one same-named type existed in `src/main`. This confidently reported that
`org.springframework.beans.TestBean` (deleted 2022) should become
`org.springframework.test.context.bean.override.convention.TestBean` — the
unrelated `@TestBean` bean-override annotation. Nonsense, and it ranked *above* the
genuine `PropertySourcesPlaceholderConfigurer` bug.

Root cause: five distinct types in this tree are named `TestBean`. A common simple
name carries no information about where anything moved.

Fix: `unambiguous` requires the simple name to be **globally unique** in the tree
**and** that sole match to be published API. Both halves are load-bearing.
Confidence is now four-valued — `unambiguous` / `ambiguous` / `test_only` / `none` —
and drives the score. Regression test:
`test_common_name_with_many_matches_is_ambiguous`.

### 3. The recon incident that became a product feature

While measuring `{spring-framework-api}` javadoc links during Phase 1, an extractor
reported **227 of 227 unresolved**. Cause: the attribute expands to a URL path that
already contains `org/springframework` (`framework-docs/antora.yml:40`), and the
code prefixed it again. Zero findings, 227 false positives, all looking like work.

This is why `MinerDefect` exists. Any extractor whose miss rate exceeds a sanity
ceiling (default 25%, measured worst real rate ~17%) refuses to emit and exits `2`.
Tested by `SelfCheckTests`.

### 4. nohttp allowlist patterns matched against the wrong thing

The allowlist entries are `^`-anchored regexes (`^http://jibx.sourceforge.net.*`).
The first implementation tested them against raw file text, where the URL sits
mid-line, so every anchored pattern failed and **all six entries were reported
dead**. nohttp actually extracts URLs and tests each *URL*. Fixed by extracting
URLs first and matching against those.

Same family of error as the recon `{spring-framework-api}` incident: the matcher
and the thing being matched were in different shapes.

### 5. The allowlist matched itself

Once URL extraction was correct, the allowlist file was still in the scan corpus —
and every pattern contains the URL it permits, so each entry matched its own
definition. The check could never report anything dead, permanently and silently.
Fixed by excluding the config file from its own scan corpus.

This one is worth dwelling on: it produced a *clean-looking* result (0 dead
entries) that happened to agree with a manual spot-check, and would have passed
review. It was only caught because a fixture test asserted a known-dead entry.

### 6. Self-check blind spot on small files

Bugs 4 and 5 each condemned or spared 100% of a 6-entry file, and the sanity
ceiling did not fire because the sample was below the 20-entry minimum. Added an
independent rule: **every entry in a config file being dead is implausible at any
size** (floor of 3). The rate ceiling still handles larger samples.

Corollary recorded for future miners: a percentage-based self-check needs an
absolute companion, or small corpora slip through.

### 7. Unused antora attributes: scoping rule instead of a denylist

Added as a third source in `config_dead_entry`, on precedent found in the review
corpus (PR #31619, a maintainer proposing removal of the unreferenced `issues-old`
attribute and deleting the obsolete sections himself).

The first shell measurement returned **14**; the real answer is **5**. The
extractor had matched 4-space-indented keys anywhere in `antora.yml`, sweeping in
the unrelated `ext:` block (`run`, `scan`), Asciidoctor built-in settings
(`chomp`, `fold`, `table-stripes`, `attribute-missing`), and attributes consumed by
the `include-code` extension rather than by `{}` substitution (`include-java`,
`include-kotlin`, `include-xml`).

Two fixes, and the second is the more interesting:

1. Parse the `asciidoc: attributes:` block by indentation state rather than by a
   loose regex over the whole file.
2. **Scope to link-valued attributes** — value starts with `http(s)://` or `{`
   — instead of maintaining a denylist of built-in names. This is self-justifying:
   `chomp: 'all'` and `include-java: 'example$docs-src/...'` are settings and
   resource paths, not links, so they fall out of scope without being named, and
   the rule does not rot as Asciidoctor adds settings.

Preferring a scoping rule to a name denylist is worth generalising. The docs miner
still uses a denylist (`org.springframework.{samples,ws,data,…}`) because no
comparable structural property distinguishes other Spring projects; where such a
property exists, it is the better tool.

Deliberate limitation: no cascade. If a dead attribute is the sole referrer of
another, the second stays hidden until the first is removed. Chasing the transitive
closure would propose removals justified only by an unmerged change.

Verified independently by grep: all five at 0 usages, against a live control
(`{spring-framework-api}`, 307 usages).

### 8. `--since` with a bare date drifts with the time of day

Two `docs_drift` fixture tests failed one morning after passing the previous
afternoon, with zero code changes in between. Cause: `_signature_changes` used
`git log --since=<doc-date>`, and git's date parser completes a bare date with
the **current wall-clock time**. Fixture commits are stamped at noon, so before
noon the doc's own initial commit fell inside the window and injected an
uncancellable `+` signature line; after noon it fell outside and everything
cancelled. The live harvester had the same wobble for any page whose doc and
code commits share a day — a direct violation of "same commit in, same output
out" that the test suite could only catch by being run at the right time of day.

Fix: no date parsing at all. The doc's last substantive *commit* was already
recorded, so the query is now the graph range `doc_commit..HEAD`, which asks the
actual question exactly. Re-harvesting the real tree produced the same 9 pages
in the same order (counts shifted marginally: `factory-nature` 9 → 8 changes),
so no prior conclusion rested on the wobble.

## Categories killed by measurement, after Phase 1

Tested while scoping the second miner, all deterministic, all dead ends:

| Hypothesis | Result |
|---|---|
| Broken `xref:` links between doc pages | 1702 targets, **0** unresolved |
| `include-code::` targets missing | 216 targets, **0** missing — they compile, so cannot drift |
| Doc pages whose referenced code changed later | 22 pages, but **signal contaminated**: all report `doc last = 2025-07-10`, a bulk docs commit. Same failure mode as the PR `updated_at` reset. Usable only with bulk commits (>50 files) excluded. |

## Categories rejected — index

Full evidence in `01-phase1-recon.md`. Rejected outright: assertion style, test
naming, `public` `@Test`, `@since` presence, class javadoc presence, `package-info`
presence, copyright drift, javadoc `@link` refs, internal `@Deprecated(forRemoval)`
call sites, `XXX`/`HACK` markers. Every one is already machine-enforced or its
residue is deliberate and catalogued in `checkstyle-suppressions.xml`.

Deprioritised: stalled PRs (discovery signal destroyed by a bot event; mergeability
blocked by DCO). Narrow single-PR items, not campaigns: `Thread.sleep` in
`EnableSchedulingTests.java`, ~5 bare `@Disabled` sites.

## Judgement calls worth flagging

**Three of the nine emitted candidates are lower-value than their scores suggest at
first glance**, and the tool says so rather than hiding it. `TestBean` (×2),
`DerivedTestBean` and `AnnotationTestBean` are illustrative doc examples naming
types that were never published API. They *are* genuinely dead references — a
reader copying that XML gets `ClassNotFoundException` — but repairing them is an
editorial decision for the docs team, not a mechanical package correction. They are
scored 55–74 against 85 for the real bug, flagged `illustrative_only`, and the
summary states plainly that the fix is editorial. One has been declined in the
ledger as a worked example of the suppression path.

**The strongest single candidate is `factory-extension.adoc:463`.** A one-token
package correction, docs-only, zero behaviour risk, objectively verifiable, and
wrong since 2011. If only one PR is ever opened from this tool, it should be that one.
