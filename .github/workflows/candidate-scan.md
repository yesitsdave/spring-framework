---
description: >
  Weekly scan: deterministic miners find candidate work, then an agent files
  the top new candidates as GitHub issues with their evidence packs.

on:
  schedule: weekly on monday
  workflow_dispatch:

permissions:
  contents: read
  issues: read
  actions: read

engine: claude

network: defaults

timeout-minutes: 20

steps:
  - name: Check Out Code
    uses: actions/checkout@v6
    with:
      fetch-depth: 0
      persist-credentials: false
  # Explicit step rather than the frontmatter `cache:` field: the compiler
  # appends that one after the custom steps, which would restore the GitHub
  # API cache only after the harvest that needs it has already run.
  - name: Restore GitHub API cache
    uses: actions/cache@v6
    with:
      key: candidate-miner-github-cache-${{ github.run_id }}
      path: tools/candidate-miner/out/.cache/github
      restore-keys: |
        candidate-miner-github-cache-
  - name: Mine and rank proposable candidates
    id: scan
    working-directory: tools/candidate-miner
    env:
      GITHUB_TOKEN: ${{ github.token }}
    run: python3 scan.py --repo-slug "$GITHUB_REPOSITORY" --max 3

# staged: true is the maiden-run setting — issues are rendered as a preview in
# the run summary instead of being created. Remove it (and `gh aw compile`)
# once a preview has been reviewed and looks right.
safe-outputs:
  staged: true
  create-issue:
    max: 3
    title-prefix: "[candidate-miner] "
    labels: [candidate-miner]

---

# Candidate scan: file mined candidates as issues

You are the issue-filing half of a two-part pipeline. The deterministic half
has already run: `tools/candidate-miner/out/proposable.json` now contains the
candidates that (a) the miners found at this commit, (b) are not declined in
the committed ledger, and (c) have no existing GitHub issue. Your only job is
to turn each of them into a well-written GitHub issue.

## Hard rules

- Work **only** from `tools/candidate-miner/out/proposable.json`. Do not run
  the miners yourself, do not add candidates, do not substitute your own
  judgement about what is or is not a real finding.
- If the file is missing or its `candidates` array is empty, stop immediately
  and produce no outputs.
- Copy each candidate's `fingerprint` **byte-for-byte** into the issue body
  marker. A mistyped fingerprint breaks deduplication permanently.
- Quote evidence verbatim from the record. Never invent commit SHAs, dates,
  URLs, line numbers, or "probably" claims that are not in the record.
- One issue per candidate. At most 3 issues.

## For each candidate in `candidates` (already ranked; keep the order)

You may read the files named in the candidate's `locus` and `evidence` to
understand the surrounding context before writing — this is encouraged, it
makes the issue concrete — but the claim itself must stay exactly what the
record says.

Create one issue via the `create-issue` safe output:

**Title**: one line, concrete, under 80 characters. Name the thing and the
place, not the tool. Good: `Docs reference PropertySourcesPlaceholderConfigurer
under its pre-2011 package`. Bad: `candidate-miner finding docs.dead_api_reference`.

**Body**, in this order:

1. **What is wrong** — two or three sentences a maintainer can verify by
   opening the file. State what a reader of the docs / user of the config
   actually experiences (e.g. copying the snippet throws
   `ClassNotFoundException`).
2. **Where** — the file and advisory line from `locus`, as a repo-relative
   path, plus the occurrences from the evidence.
3. **Evidence** — the relevant fields of the record, quoted verbatim in a
   fenced block: the dead reference, relocation target or deletion commit if
   present, extractor, and score with its inputs.
4. **Suggested fix** — one short paragraph. For an unambiguous relocation:
   state the one-token change. For a deleted feature: removing the section is
   the fix, and say the docs team may prefer their own wording. Do not write
   the patch.
5. **How to verify the fix** — run the named miner from
   `tools/candidate-miner/` and confirm this fingerprint is no longer emitted:
   `./miner.py run --miner <miner> --dry-run`.
6. **Provenance** — the commit the candidates were mined at (`repo.commit` in
   the record) and, for `flaky_test` candidates, the note that evidence
   reflects the harvest window recorded in the record.
7. The marker, exactly:
   `<!-- candidate-miner:fingerprint <fingerprint> -->`

Write for a junior contributor picking up their first issue on this repo:
plain sentences, no internal jargon from the mining tool beyond what the
evidence block already shows, and no exclamation marks.
