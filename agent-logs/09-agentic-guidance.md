# 09 — Agentic guidance: turning the corpus into an AGENTS.md draft

The review-comments corpus (§04, §08) has so far produced *negative* knowledge —
prohibitions, killed rules, the vendored-code interlock. This step converts it
into the deliverable the brief explicitly asks for ("adding agentic guidance and
skills"): a draft `AGENTS.md` for the repository, written for both coding agents
and new human contributors.

Artifact: `tools/candidate-miner/guidance/AGENTS.md`.

## Placement decision

The draft is a **recommendation, not a repo file**. Two reasons it lives under
`tools/candidate-miner/guidance/` rather than the repository root:

1. The project constraint: no additions or modifications to Spring source,
   tests, docs, or build files. A root `AGENTS.md` is exactly such an addition —
   it is the *proposal*, and adopting it is the maintainers' call.
2. Provenance. Sitting beside the corpus that generated it, every citation in
   the document can be checked against `out/review_comments.corpus.jsonl` with
   grep. A guidance file whose rules can be audited line-by-line is the whole
   point; detaching it from its evidence would undo that.

## Derivation rules (same evidence bar as the miners)

- Every behavioural rule cites the PR(s) whose review comments state it.
- Where we measured a rule against the tree (§08), the measurement is in the
  document — including the two cases where measurement *weakens* the rule
  (`final` locals: 713 counterexamples; `</li>` closing: 72% of the tree
  disagrees). Those are marked as new-code bars, not cleanup mandates, precisely
  so no agent turns them into a 700-file "fix" campaign.
- Process facts cite file:line in the checkout (`CONTRIBUTING.md:68`, `:87`,
  `:127-135`; `.github/actions/build/action.yml:49`) — re-verified this session,
  not quoted from earlier notes.
- The single-maintainer skew (54 of 61 comments) is disclosed in the header. A
  reader must be able to see that "style rules maintainers enforce" mostly means
  one maintainer's consistently-applied lens.

## What made it in vs. not

In: the presumed-deliberate norm with its seven PR citations and the
bring-evidence exception; the vendored-package prohibition verbatim; one-concern
PR scoping (both directions: no drive-bys, but full-sweep when a pattern change
is requested); the enforced style rules; test-writing rules; docs-PR mechanics;
five agent-specific directives that operationalize the above.

Out, deliberately:

- Anything Checkstyle already enforces (assertion library, naming, imports,
  headers). Restating machine-enforced rules is noise; the document says "run
  checkstyle" once instead.
- Rules we could not source. There is plenty of Spring lore (commit message
  format, `@since` tagging habits, backport labels) that I *believe* to be true
  from prior knowledge — none of it appears, because none of it is in the corpus
  or the checkout. The document's authority comes from being 100% traceable; one
  unsourced rule would poison that.
- The `assertThatCode` rule was kept even though §08 judged it unminable —
  unminable-by-grep and untrue are different things; as guidance for *new* code
  it is directly evidenced (#25239).

## Why this is the right shape for the "agentic guidance" brief item

The interlock finding (§08) showed that an agent armed with a true style rule
and no prohibitions produces 34 confident, worthless PRs. This document is the
inverse artifact: prohibitions first, evidence bar second, style third. It is
what you hand a coding agent *before* letting it propose changes — and its five
closing directives are written to be machine-followable, not aspirational.
