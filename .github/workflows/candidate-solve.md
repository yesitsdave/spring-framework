---
description: >
  Solve a mined candidate: applying the `solve-it` label to a candidate-miner
  issue verifies the candidate still exists, makes the minimal fix, proves the
  candidate gone, and opens a draft PR.

on:
  label_command:
    name: solve-it

permissions:
  contents: read
  issues: read
  pull-requests: read

# Same model constraint as candidate-scan: must be in the AWF api-proxy's
# curated pricing table (claude-opus-5 is missing from v0.27.42's).
engine: claude
model: claude-opus-4-8

network: defaults

timeout-minutes: 30

# The bash entry needs the `:*` suffix (Claude Code permission syntax) to
# permit arguments — a plain trailing ` *` is silently dropped by the
# compiler, leaving only the bare command allowed, which auto-denies the
# mandated `--fingerprint` verification call.
tools:
  edit:
  bash:
    - "python3 tools/candidate-miner/solve_target.py:*"

steps:
  - name: Check Out Code
    uses: actions/checkout@v6
    with:
      fetch-depth: 0
      persist-credentials: false
  - name: Verify the target candidate
    id: verify
    working-directory: tools/candidate-miner
    env:
      GITHUB_TOKEN: ${{ github.token }}
    run: python3 solve_target.py --repo-slug "$GITHUB_REPOSITORY" --issue "${{ github.event.issue.number }}"

# staged: true is the maiden-run setting — the PR and comment are rendered as
# a preview in the run summary instead of being created. Remove it (and
# `gh aw compile`) once a preview has been reviewed and looks right.
safe-outputs:
  create-pull-request:
    draft: true
    title-prefix: "[candidate-solve] "
    labels: [candidate-miner]
  add-comment:
    max: 1
---

# Candidate solve: fix one verified candidate and open a draft PR

A human applied the `solve-it` label to a candidate-miner issue. The
deterministic verifier already ran: `tools/candidate-miner/out/solve_target.json`
holds its verdict for this issue's candidate. Your job depends entirely on
that verdict.

## First: read the verdict

Read `tools/candidate-miner/out/solve_target.json`. If `verdict` is anything
other than `confirmed`, do **not** change any file. Post one comment via the
`add-comment` safe output explaining the situation plainly, then stop:

- `gone` — the candidate no longer exists at HEAD; the issue is already fixed
  and can be closed.
- `declined` — a human permanently vetoed this candidate in the ledger
  (include the recorded reason); the issue should be closed, not solved.
- `unsupported` — the evidence is historical (a flaky-test or adjudicated
  finding), so a mechanical fix cannot be proven here; a human needs to take
  this one.
- `no_marker` / `ambiguous_marker` / `unknown_fingerprint` — the issue is not
  traceable to exactly one mined candidate; say what is wrong with it.

## If `confirmed`: make the fix

Read `AGENTS.md` at the repository root first and obey it throughout —
especially: presume existing code is deliberate, never touch
`org.springframework.{asm,cglib,objenesis}`, and keep the diff to this one
concern. Also:

- The `candidate` record in `solve_target.json` is your complete
  specification. Edit **only** the file named in its `locus`, and only the
  occurrences listed in its `evidence`.
- `docs.dead_api_reference` with `relocation_confidence: "unambiguous"`:
  change the package portion of the reference to the single
  `relocated_to_public_api` entry. Nothing else — not the prose around it.
- `config.dead_entry`: delete the dead entry (the whole suppression line,
  attribute line, or allowlist entry), nothing adjacent.
- Any other shape — an ambiguous relocation, an `illustrative_only` flag in
  the evidence, a removal that needs editorial judgement about surrounding
  prose — is **not yours to guess**: revert anything you changed, post one
  comment explaining exactly what editorial decision a human needs to make,
  and stop without creating a PR.
- Never modify anything under `tools/candidate-miner/` (including its
  ledgers), `agent-logs/`, or `.github/`.

## Then: prove it

Run the offline post-fix check with the fingerprint from `solve_target.json`:

```
python3 tools/candidate-miner/solve_target.py --fingerprint <fingerprint>
```

Re-read `tools/candidate-miner/out/solve_target.json`. The fix is proven only
when `verdict` is now `gone`. If it is still `confirmed`, your edit did not
remove the candidate: revert your changes, post one comment saying the
automated fix failed verification, and stop without creating a PR.

## Finally: the draft PR

Create it via the `create-pull-request` safe output:

- **Title**: one line naming the fix itself, under 72 characters — e.g.
  `Fix PropertySourcesPlaceholderConfigurer package in factory-extension docs`.
- **Body**:
  1. What changed and why, in two or three sentences a reviewer can verify
     against the diff.
  2. `Fixes #<issue number>`.
  3. **Verification** — state that the owning miner emitted this candidate's
     fingerprint before the fix and no longer emits it after
     (`solve_target.py` verdicts `confirmed` → `gone`), and quote the
     fingerprint.
  4. **For the reviewer** — one sentence on what human judgement is still
     needed (e.g. whether the docs team prefers different surrounding
     wording).
- Do not post an `add-comment` when a PR was created — the PR link on the
  issue is the notification.

Write plainly; no exclamation marks; the diff should speak for itself.
