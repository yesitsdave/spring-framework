# 08 — Deep review-comment harvest: 934 merged PRs

Second, deeper run of the Spec A harvester. The shallow run (§04) covered 300
merged PRs; this one covers 934 — effectively the reachable history.

```
prs_listed                     5718
prs_merged                      934      (16% of closed PRs merge)
prs_with_changes_requested       34      (3.6% of merges)
comments_seen                   131
comments_kept                    61      sbrannen 54 · bclozel 5 · snicoll 2
```

61 comments across 23 PRs, 15 of them new since §04. Still readable directly —
**no clustering model was needed at 61 any more than at 30.** Worth noting the
skew: 54 of 61 comments are from one maintainer, so extracted "team rules" are
increasingly one person's preferences. That, not corpus size, is the reason to
stop harvesting deeper.

The harvester also reported its own coverage limit honestly: `--limit` was 1200
but only 934 merged PRs were reachable.

## The largest cluster is a hard prohibition

**Do not modify repackaged third-party code.** Six comments across four separate
PRs (#1943, #23478, #24933, #25450), each quoting the same formal policy:

> *"Please refrain from modifying classes under `org.springframework.asm`,
> `org.springframework.cglib`, and `org.springframework.objenesis`. Those include
> repackaged forks of..."*

This is the single largest cluster in the corpus and the most operationally
important thing found in the entire project. It is a **negative** rule — nothing
to mine, everything to exclude — and any workflow that proposes changes must
enforce it.

Verified against the shipped miners: `docs_dead_ref` and `config_dead_entry` emit
**zero** candidates touching those packages. Checked, not assumed.

## The interlock — the most instructive result in the project

PR #1784 states a convention explicitly:

> *"We generally name exceptions `ex` instead of `e`."*

Measured across `spring-*/src/main/java`:

| | |
|---|---|
| `catch (… ex)` | **2192** |
| `catch (… e)` | **34** |
| adherence | **98.5%** |

A textbook "94% convention" — discovered from maintainer comments, stated
explicitly, and with a small, mechanically-findable residue. It looks like an
ideal miner.

Then the split:

| | |
|---|---|
| offenders in vendored `cglib` / `asm` | **34** |
| offenders in Spring-authored code | **0** |

**Every single one is in code the team has a written policy against touching.**
Actionable inventory: zero.

The two findings interlock, and that is the lesson. A miner built on the first
rule alone would have produced 34 confident, well-evidenced, trivially-diffable
candidates — and every one would have been closed on sight with a quoted policy.
The rule that kills them was discovered in the *same corpus*, from different PRs.
Mining rules without also mining prohibitions produces work that looks perfect
and is entirely wasted.

## Rules measured and rejected

| Rule (source) | Measurement | Verdict |
|---|---|---|
| Exceptions named `ex` not `e` (#1784) | 2192 vs 34; **all 34 vendored** | real rule, **0 actionable** |
| No `final` on local variables (#24683) | **713** instances | 713 is not a 6% residue — the convention is not strongly held. Rejected. |
| Javadoc `<li>` needs closing `</li>` (#22777) | 1318 of 1824 (**72%**) have no same-line close | the majority form — not a repo convention at all. The comment was about one PR's javadoc. Rejected. |
| `assertThatCode(…).doesNotThrowAnyException()` (#25239) | not measured | the negative case (an assertion that simply calls code) is not reliably detectable by grep. Rejected as unminable. |

## "Please revert — this was intentional" keeps growing

Reinforced across the deeper corpus: #23478 (×2), #24789, #25448 (×3), #24933,
on top of #28426 and #35163 from §04. Contributors' unsolicited "improvements" —
grammar, redundant-looking checks, obsolete-looking documentation, structural
tidying — are rejected as deliberate. In #23478 the maintainer explains a
technically-equivalent change is refused because *"it breaks with the structure of
the rest of the method."*

One nuance worth keeping: in the same PR the maintainer **accepted** a change
after the contributor supplied benchmark evidence ("I had momentarily forgotten
about those findings from Shipilёv. Thanks for the links. We'll keep the proposed
change!"). The bar is not "never change working code" — it is **"bring evidence."**
Any proposal a workflow generates should carry its evidence in the PR body.

## Net result of the deeper harvest

Three new minable rules found, **all three measured to zero actionable inventory**;
one hard prohibition found and verified against the shipped miners; and the
"presumed deliberate" negative rule substantially reinforced.

Consistent with everything else in this project: the corpus is genuinely useful,
and what it mostly produces is *reasons not to act*. That is the correct output
for a repo where 10 of 13 original candidate categories died against existing
enforcement.
