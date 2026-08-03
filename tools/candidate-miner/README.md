# candidate-miner

Deterministic mining for engineering-excellence work suitable for junior
contributors to Spring Framework.

**No LLM calls anywhere in this tool.** Same commit in, same bytes out, stable
ordering. Agentic triage happens downstream, against this tool's output.

## Why this exists

Spring Framework is mature and maintained by a small, exacting core team. The
scarce resource is not work — it is maintainer review attention. The
`status: ideal-for-contribution` label currently has **zero** open issues,
because easy work that maintainers already know about gets fixed by maintainers.

The niche is therefore work where **discovery cost exceeds fix cost**. Nobody
audits 845k lines and 470 AsciiDoc pages for a convention applied in 94% of
places, but each of the remaining 6% is a two-minute fix with an obvious diff.

Precision matters far more than recall. Five real, mergeable candidates and 200
correctly discarded is a better outcome than 500 hits.

## The targeting principle: mine the enforcement shadow

This repo already machine-enforces most of what a grep could find. Checkstyle
(`src/checkstyle/checkstyle.xml`, 287 lines) bans non-AssertJ assertion APIs
outright, requires `package-info.java`, enforces `*Tests` class naming, validates
`@since` *format*, and much more. Violations cannot accumulate where a build gate
is watching.

So rot concentrates exactly where no tool looks, and the boundary is drawn in the
build config itself:

| Zone | Enforcement | Measured drift |
|---|---|---|
| `spring-*/src/main/java` | checkstyle + ArchUnit + NullAway + javac `-Werror` | `@since` 100%, javadoc refs 99.985% |
| `src/test`, `src/testFixtures` | javadoc checks suppressed (`checkstyle-suppressions.xml:12`) | all broken FQN javadoc refs found live here |
| `framework-docs/**` | **none** — `build.gradle:14` excludes `framework-*` from the convention plugins | highest drift |

`antora` does run on pull requests, but it only *renders* AsciiDoc; it never
checks that a Java FQN in a snippet still exists. Meanwhile 216 `include-code::`
directives bind to compiled sources and cannot drift, while ~1169 inline
`[source,java]` blocks compile against nothing. That split is the fault line.

## Install

Nothing to install. Python 3.11+, standard library only.

This tool is deliberately **absent from `settings.gradle`**, so Gradle never sees
it. It is not a Gradle plugin and is not in the build graph. It only ever reads
the tree it scans.

## Use

```bash
cd tools/candidate-miner

./miner.py list                                  # registered miners, by cost class
./miner.py run --miner docs_dead_ref             # human summary + write ledger/output
./miner.py run --miner docs_dead_ref --dry-run   # report only, write nothing
./miner.py run --miner docs_dead_ref --format jsonl > candidates.jsonl

./miner.py harvest --source review_comments --limit 300   # needs a GitHub token

./miner.py corpus list                           # what has been harvested
./miner.py corpus show --source docs_drift       # read a corpus
./miner.py corpus show --source review_comments --limit 10

./miner.py ledger show                           # all miners' ledgers
./miner.py ledger show --state new
./miner.py ledger queue    <fingerprint>
./miner.py ledger done     <fingerprint>
./miner.py ledger decline  <fingerprint> --reason "why" --by you
```

`--include-experimental` enables lower-precision extractors. Off by default:
precision over recall.

Exit codes: `0` success, `1` error, `2` miner defect (see below).

## Cost classes

Miners declare how expensive they are to run, so later scheduling can treat them
completely separately. They are never coupled.

| Class | Meaning | Miners |
|---|---|---|
| `a_api` | remote API data, no checkout scan | `flaky_test` (consumes the `ci_failures` corpus) |
| `b_source` | needs a checkout, no build | `docs_dead_ref`, `config_dead_entry` |
| `c_build` | needs a Gradle build | *(none yet)* |

The two source miners answer the same question against different corpora: **this
references that — does that still exist?** `docs_dead_ref` asks it of AsciiDoc
pages; `config_dead_entry` asks it of checkstyle suppressions, the nohttp allowlist
and Antora attributes, where a stale entry keeps working silently forever after its
target is deleted. `flaky_test` is the third: it consumes a harvested `ci_failures`
corpus and emits tests that failed on otherwise-green main in two or more distinct
runs. Corpora measured and found *clean* — `xref:` links (1702 refs, 0 dead)
and `include-code::` directives (216, 0 dead, because they compile) — are
deliberately not mined.

## Harvesters

A harvester is **not** a miner. Miners emit scored `Candidate` records for triage;
harvesters emit a *corpus* that a downstream step reasons over. Still no LLM here —
that stays outside this tool.

Three corpora are available:

| Corpus | Cost class | What it collects |
|---|---|---|
| `review_comments` | `a_api` | maintainer review comments that caused a change |
| `docs_drift` | `b_source` | doc pages whose documented types gained/lost public API since |
| `ci_failures` | `a_api` | failed main-branch CI runs, classified, failing tests extracted |

`ci_failures` feeds the `flaky_test` miner and doubles as CI-quality evidence in
its own right (non-test failures — javadoc, antora, nohttp — are classified and
kept rather than dropped).

`docs_drift` is a reading list, not a defect list: nothing deterministic can prove a
paragraph no longer describes its subject. Its value is the funnel — 464 pages to 9,
after excluding bulk commits, requiring a public-signature change, and cancelling
annotation-only churn (71% of raw signature changes here were JSpecify migration
noise).

`review_comments` collects maintainer review comments from PRs that were **merged
only after changes were requested**, which isolates comments that demonstrably
caused a change: a rule the team enforces that the contributor did not know. It is
the antidote to guessing what this team would merge, which Phase 1 showed goes
badly.

Needs a token — `gh auth login` or `GITHUB_TOKEN`. Unauthenticated access is 60
requests/hour, and the command refuses to run without one unless you pass
`--allow-unauthenticated`, because a truncated corpus is worse than none. All
responses are cached under `out/.cache/github`, so re-runs are free and identical.

## The scan workflow

`.github/workflows/candidate-scan.md` (a [gh-aw](https://github.github.com/gh-aw/)
workflow) runs weekly. Its deterministic half is `scan.py`:

```bash
./scan.py --repo-slug <owner>/<repo> --max 3
```

which harvests, runs every miner, drops candidates that already have a GitHub
issue — open **or closed**, so closing an issue is a veto the scan respects —
ranks the rest by score, and writes the top `--max` to `out/proposable.json`.
The workflow's agent may only file issues for what that file contains; each
issue body ends with a visible fingerprint marker
(`` `candidate-miner:fingerprint sha256:...` ``) that is the deduplication
key for every later run. It is deliberately *not* an HTML comment — gh-aw's
safe-output sanitizer strips those from agent-authored bodies.

The issue listing is deliberately fetched uncached (unlike harvest traffic): a
cached listing would miss issues filed after the cache was written and
re-propose their candidates. A failed harvest skips only the miners that
consume it, with a recorded warning; a miner failure stops the scan loudly.

## The solve and reconcile workflows

`candidate-solve` (`.github/workflows/candidate-solve.md`, gh-aw): applying
the `solve-it` label to a candidate-miner issue runs `solve_target.py`, which
resolves the issue's fingerprint through the committed ledger to its owning
miner and re-runs it. Only on a `confirmed` verdict does the agent make the
minimal fix — then it must re-run `solve_target.py --fingerprint <fp>`
(offline) and see `gone` before a draft PR is created. The same tool proves
the candidate's presence before the fix and its absence after.

`candidate-ledger-reconcile` (`.github/workflows/candidate-ledger-reconcile.yml`,
plain Actions, no LLM): `reconcile.py` folds issue outcomes back into the
committed ledgers and the workflow commits the result. The gesture → state
mapping:

| Issue gesture | Ledger state |
|---|---|
| open | `queued` |
| closed as completed (e.g. by a merged PR's `Fixes #N`) | `done` |
| closed as not planned | `declined` — permanent, with who and why |
| reopened after done | `queued` again |
| anything vs. an existing `declined` | held and reported; only a hand edit reopens a veto |

Issues created by the scan's own token never fire the event trigger (GitHub
suppresses workflow-triggering for `GITHUB_TOKEN` events — which also means
no workflow loops); the weekly sweep reconciles those.

## The ledger

`ledger/<miner>.jsonl` persists one entry per fingerprint with state
`new` → `queued` → `done`, plus `declined`. One additional ledger is written by
`triage.py` rather than a miner: `ledger/adjudicated_docs_drift.jsonl`, which
holds model-adjudicated docs-drift candidates and follows the same state
machine and record schema.

**`declined` is terminal.** A human declining a candidate suppresses it
permanently; the tool will not reopen it. This is the rule everything else here
serves — repeat runs diff rather than re-propose.

The ledger is written sorted, one record per line, and `save()` is a no-op when
content is unchanged, so a scheduled job never commits an identical file.

**This tool never touches git.** It reads the tree, writes JSONL, exits.
Committing the ledger is the caller's business, which keeps the miner testable
offline and leaves CI conventions to the workflow layer.

### Renames

If a file moves, its candidates get new fingerprints and any decision recorded
against the old path is orphaned. The tool **reports** these rather than guessing:

```
POSSIBLE RENAMES - needs a human
```

Auto-carrying a decline to a same-looking candidate at a new path was
deliberately not implemented. It can silently suppress the wrong file, and a
wrong suppression fails by omission — the worst failure mode for a
precision-first tool.

## Miner defects

A miner that detects an implausible result **refuses to emit** and exits `2`:

```
MINER DEFECT: extractor 'api_url' reports 227/227 references unresolved (100%),
above the 25% sanity ceiling. This almost certainly means the extractor is
broken -- most likely FQN normalisation -- not that the docs are.
```

This is not hypothetical. During reconnaissance an extractor did report 227 of
227 javadoc links dead, because the `{spring-framework-api}` attribute already
expands to a path containing `org/springframework` and the code prefixed it
again. A precision-first tool must fail loudly rather than emit a wall of
false positives that look like findings.

Override with `--sanity-ceiling` when a high rate is genuinely expected.

## Contract

One JSONL record per candidate, sorted by fingerprint.

```json
{
  "schema_version": 1,
  "fingerprint": "sha256:3221c545…",
  "category": "docs.dead_api_reference",
  "cost_class": "b_source",
  "miner": {"name": "docs_dead_ref", "version": "1.0.0"},
  "repo": {"commit": "1d1aac3674…", "branch": "main"},
  "locus": {"kind": "file_span", "path": "…/factory-extension.adoc",
            "line": 463, "line_is_advisory": true},
  "identity": "org.springframework.beans.factory.config.PropertySourcesPlaceholderConfigurer",
  "evidence": {
    "reference": "org.springframework.beans.factory.config.PropertySourcesPlaceholderConfigurer",
    "primary_extractor": "xml_class_attr",
    "relocation_confidence": "unambiguous",
    "relocated_to_public_api": ["org.springframework.context.support.PropertySourcesPlaceholderConfigurer"],
    "removed_in": null,
    "occurrences": [{"line": 463, "text": "<bean class=\"…\">"}]
  },
  "pr_grouping_key": "…/factory-extension.adoc",
  "score": {"value": 85, "inputs": {"extractor_precision_prior": 40, "…": 25}}
}
```

**Fingerprint** = `sha256(category ‖ path ‖ identity)`. Line numbers are
deliberately excluded so a candidate survives arbitrary drift, and occurrence
counts are excluded so adding or removing a repeat of the same dead reference in
one file does not mint a new identity.

**No wall-clock timestamps in records.** Run metadata goes to
`out/<miner>.manifest.json`. Two runs against the same commit therefore produce
byte-identical JSONL, so `diff` shows real change and nothing else.

**Score** is a deterministic integer weighted sum with every input exposed;
validation enforces that the total equals the sum of its parts, so it is always
auditable. Integers only — floats would vary in their last bits across platforms.

**Granularity**: one dead reference in one file is one record, however many times
it appears — it is a single fix. `pr_grouping_key` lets downstream triage batch a
whole file together.

## Adding a miner

Implement the protocol in `candidateminer/miners/base.py`:

```python
class MyMiner:
    name = "my_miner"
    cost_class = CostClass.B_SOURCE
    version = "1.0.0"
    description = "one line"

    def run(self, ctx: MinerContext) -> Iterable[Candidate]:
        ...

MINER = MyMiner()
```

Register it in `candidateminer/miners/__init__.py`. `ctx.type_index` is built
once and shared. Sort output by fingerprint. Raise `MinerDefect` rather than
emitting results you do not believe.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Unit tests build synthetic trees in temp directories and never touch the real
checkout. `test_golden_repo.py` runs against the actual repo and asserts mostly
*properties* — nothing denylisted, nothing that actually resolves, every record
valid — plus a small pinned set of known-dead references as a canary. The pin is
deliberate but has a cost: when a maintainer fixes one of those docs (the
outcome this tool exists to cause), the canary list must be updated to match.

## Layout

```
miner.py                       deterministic CLI (never imports LLM code — tested)
triage.py                      adjudication CLI (corpus -> model -> ledger)
scan.py                        workflow support: miners -> dedupe vs filed issues
                               -> out/proposable.json (see "The scan workflow")
solve_target.py                workflow support: issue -> ledger -> owning miner
                               -> confirmed/gone verdict (see "The solve and
                               reconcile workflows")
reconcile.py                   workflow support: issue outcomes -> ledger states
                               (deterministic; committed by its workflow)
candidateminer/
  contract.py                  record schema, fingerprint, validation
  index.py                     FQN index + resolution + denylist
  ledger.py                    state machine, merge, orphan detection
  scoring.py                   deterministic integer scoring
  report.py                    human-readable summary
  gitutil.py                   read-only git queries (evidence, never writes)
  github.py                    cached GitHub API client (cost class a_api)
  adjudication.py              packets, verdict validation (deterministic)
  adjudicators.py              the ONLY module that calls a model (lazy SDK import)
  miners/
    base.py                    Miner protocol, MinerContext, MinerDefect
    docs_dead_ref.py           dead Spring API references in .adoc
    config_dead_entry.py       checkstyle/nohttp/antora entries with no live target
    flaky_test.py              tests failing repeatedly on otherwise-green main
  harvesters/
    base.py                    Harvester protocol, HarvestDefect
    review_comments.py         maintainer review comments from merged PRs
    docs_drift.py              doc pages whose cited code moved after the doc froze
    ci_failures.py             failed main-branch CI runs, classified, tests extracted
ledger/                        committed state
out/                           derived output (gitignored)
../../AGENTS.md                agent/contributor guidance derived from the
                               review corpus (drafted here as guidance/, then
                               promoted to the repo root when the solve
                               workflow became its first consumer; see
                               agent-logs/09 and /15)
```
