# 00 — Expectations, recorded BEFORE exploration

**Written 2026-07-27, before any grep against the checkout.** Transcribed verbatim from
the pre-exploration record. Nothing here has been edited in light of what was found —
that is the point. The scored version lives in `01-phase1-recon.md`.

Prior assumption stated up front: Spring knowledge is ~6.x-stale; Spring Framework 7.0
shipped Nov 2025, so the 6→7 delta is exactly where I expected to be confidently wrong.
JSpecify nullability migration was excluded from consideration on instruction (complete in 7.0).

## Per-category expectations

| Category | Expectation | Confidence |
|---|---|---|
| Unresolved `@link`/`@see` | Low count. Javadoc task likely runs with strict-ish settings in CI; broken links fail the build. Expect a dead end unless doclint is relaxed. | med |
| Missing `@since` on new public API | Real signal. `@since` is convention, not enforced by any tool I know of. Expect 1–5% of recently-added public types to lack it. | high |
| Internal use of own `@Deprecated(forRemoval=true)` | Real but small. Maintainers usually clean these at major boundaries; 7.0 just shipped so most should be gone. Expect <20 hits, many legitimate (deprecated method delegating to deprecated method). | med |
| `@Disabled` without issue link | Real signal, small. Expect 20–60 `@Disabled` total, maybe half with no explanation. | med |
| `Thread.sleep` in tests | Expect 100+ hits. Mostly *legitimate* (scheduling, async, websocket tests genuinely need time). Low precision — likely noise unless narrowed hard. | med |
| TODO/FIXME | Expect 200–500. Vast majority permanent documentation ("TODO: revisit if JDK adds X"). Blame-age will show most are >5yr. Dead end as a bulk category. | high |
| Ref-doc snippets vs real signatures | Expect real signal but *high extraction cost*. Docs are Antora/AsciiDoc; many snippets are `include::`d from real compiled test code (good practice) — those can't drift. Inline snippets can. Ratio unknown. | low |
| Stalled/abandoned PRs needing only a rebase or one comment addressed | Expect real signal. Spring has a large PR queue; some sit for months. But mergeability judgement is the hard part, not discovery. | med |

## Additional hypotheses to test (from Spring idiom knowledge, unverified)

- `package-info.java` presence per package — Spring is disciplined here; expect >95%, gaps only in new packages.
- AssertJ vs JUnit assertions in tests — Spring migrated to AssertJ years ago; expect >95% AssertJ, residue of `assertEquals` in old modules.
- Test method naming — Spring has no single enforced convention; expect this to be a dead end (too much legitimate variance).
- Copyright header year ranges — mechanically checkable, but likely already enforced or deliberately not updated.

## What I expected to be enforced already

Recorded because the brief put enforced tooling off the table, so the boundary matters:
checkstyle of some kind, spring-javaformat, and doclint. I did **not** expect the
enforcement to be broad enough to kill most of the candidate list outright.
