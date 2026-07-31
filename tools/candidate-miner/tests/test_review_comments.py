"""Review-comment harvester, driven by a fake transport. No network."""

from __future__ import annotations

import unittest

from candidateminer.harvesters.review_comments import (
    HarvestDefect,
    harvest,
)

_SUBSTANTIVE = (
    "Please use Assert.notNull here rather than an explicit if/throw, "
    "for consistency with the rest of this class."
)


class FakeClient:
    """Implements just the surface `harvest` uses.

    Deliberately has no ``get``: reviews and comments must arrive through
    ``paginate`` (the real client follows next-links there), so a regression
    back to a single-page ``get`` fails loudly rather than silently truncating
    at GitHub's 30-item default page.
    """

    def __init__(self, pulls, reviews, comments):
        self._pulls = pulls
        self._reviews = reviews
        self._comments = comments
        self.requests_made = 0
        self.cache_hits = 0

    def paginate(self, path, **_):
        resource = path.split("?", 1)[0]
        if resource.endswith("/reviews") or resource.endswith("/comments"):
            number = int(resource.rsplit("/", 2)[-2])
            source = self._reviews if resource.endswith("/reviews") else self._comments
            yield from source.get(number, [])
            return
        yield from self._pulls


def _pull(number=1, merged=True, author="contributor"):
    return {
        "number": number,
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
        "user": {"login": author},
    }


def _review(state="CHANGES_REQUESTED", association="MEMBER", login="maintainer"):
    return {"state": state, "author_association": association, "user": {"login": login}}


def _comment(
    cid=100,
    login="maintainer",
    association="MEMBER",
    body=_SUBSTANTIVE,
    user_type="User",
):
    return {
        "id": cid,
        "user": {"login": login, "type": user_type},
        "author_association": association,
        "body": body,
        "created_at": "2026-01-01T00:00:00Z",
        "path": "spring-core/src/main/java/X.java",
        "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        "html_url": "https://github.com/x/y/pull/1#discussion_r100",
    }


def _run(pulls, reviews, comments):
    return harvest(FakeClient(pulls, reviews, comments), "o/r", limit=100)


class SelectionTests(unittest.TestCase):
    def test_keeps_a_maintainer_comment_on_a_changes_requested_merge(self):
        kept, stats = _run([_pull()], {1: [_review()]}, {1: [_comment()]})
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.comments_kept, 1)

    def test_skips_prs_merged_without_changes_requested(self):
        """A frictionless merge teaches nothing about what the team requires."""
        kept, stats = _run(
            [_pull()], {1: [_review(state="APPROVED")]}, {1: [_comment()]}
        )
        self.assertEqual(kept, [])
        self.assertEqual(stats.prs_with_changes_requested, 0)

    def test_skips_unmerged_prs(self):
        kept, stats = _run(
            [_pull(merged=False)], {1: [_review()]}, {1: [_comment()]}
        )
        self.assertEqual(kept, [])
        self.assertEqual(stats.prs_merged, 0)

    def test_changes_requested_review_beyond_the_default_page_still_qualifies(self):
        """The regression: reviews came from a single get() (30-item page), so a
        heavily-reviewed PR whose CHANGES_REQUESTED review was 31st was skipped
        entirely -- along with every comment this harvester exists to collect."""
        reviews = [_review(state="COMMENTED") for _ in range(35)] + [_review()]
        kept, stats = _run([_pull()], {1: reviews}, {1: [_comment()]})
        self.assertEqual(len(kept), 1)
        self.assertEqual(stats.prs_with_changes_requested, 1)

    def test_changes_requested_by_an_outsider_does_not_qualify(self):
        kept, _ = _run(
            [_pull()], {1: [_review(association="CONTRIBUTOR")]}, {1: [_comment()]}
        )
        self.assertEqual(kept, [])


class CommentFilterTests(unittest.TestCase):
    def _kept(self, comment):
        kept, _ = _run([_pull()], {1: [_review()]}, {1: [comment]})
        return kept

    def test_rejects_non_maintainer_comments(self):
        self.assertEqual(self._kept(_comment(association="CONTRIBUTOR")), [])

    def test_rejects_bots(self):
        self.assertEqual(self._kept(_comment(login="dependabot[bot]")), [])
        self.assertEqual(self._kept(_comment(user_type="Bot")), [])

    def test_rejects_the_pr_author_reviewing_themselves(self):
        kept, _ = _run(
            [_pull(author="maintainer")], {1: [_review()]}, {1: [_comment()]}
        )
        self.assertEqual(kept, [])

    def test_rejects_trivial_bodies(self):
        for body in ("LGTM", "thanks!", "Good catch.", "+1", "okay"):
            self.assertEqual(self._kept(_comment(body=body)), [], body)

    def test_rejects_short_bodies(self):
        self.assertEqual(self._kept(_comment(body="rename this")), [])

    def test_quoted_text_does_not_count_toward_length(self):
        body = "> " + ("a very long quoted line " * 5) + "\nok"
        self.assertEqual(self._kept(_comment(body=body)), [])

    def test_keeps_a_substantive_body_containing_a_quote(self):
        body = "> your line here\n\n" + _SUBSTANTIVE
        self.assertEqual(len(self._kept(_comment(body=body))), 1)


class RecordTests(unittest.TestCase):
    def test_record_shape(self):
        kept, _ = _run([_pull()], {1: [_review()]}, {1: [_comment()]})
        record = kept[0].to_record()
        self.assertEqual(record["corpus"], "review_comments")
        self.assertEqual(record["pr"], 1)
        self.assertTrue(record["body_digest"].startswith("sha256:"))
        self.assertIn("+new", record["diff_hunk_tail"])

    def test_no_wallclock_in_record(self):
        kept, _ = _run([_pull()], {1: [_review()]}, {1: [_comment()]})
        self.assertNotIn("harvested_at", kept[0].to_record())

    def test_output_is_sorted_by_pr_then_comment(self):
        pulls = [_pull(2), _pull(1)]
        reviews = {1: [_review()], 2: [_review()]}
        comments = {
            1: [_comment(cid=9), _comment(cid=3)],
            2: [_comment(cid=5)],
        }
        kept, _ = _run(pulls, reviews, comments)
        self.assertEqual([(c.pr, c.comment_id) for c in kept], [(1, 3), (1, 9), (2, 5)])


class SelfCheckTests(unittest.TestCase):
    def test_changes_requested_but_nothing_kept_is_a_defect(self):
        """Maintainers do not request changes silently -- a filter must be wrong."""
        pulls = [_pull(i) for i in range(1, 7)]
        reviews = {i: [_review()] for i in range(1, 7)}
        comments = {i: [_comment(cid=i, association="CONTRIBUTOR")] for i in range(1, 7)}
        with self.assertRaises(HarvestDefect) as caught:
            _run(pulls, reviews, comments)
        self.assertIn("filter", str(caught.exception))

    def test_no_changes_requested_across_many_merges_is_a_defect(self):
        pulls = [_pull(i) for i in range(1, 40)]
        reviews = {i: [_review(state="APPROVED")] for i in range(1, 40)}
        with self.assertRaises(HarvestDefect):
            _run(pulls, reviews, {})

    def test_small_clean_sample_does_not_trip_the_check(self):
        kept, _ = _run([_pull()], {1: [_review(state="APPROVED")]}, {})
        self.assertEqual(kept, [])


if __name__ == "__main__":
    unittest.main()
