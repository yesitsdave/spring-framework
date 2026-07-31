"""Harvest maintainer review comments that *caused a change*.

Spec A, deterministic stage. **No LLM here.** This produces a corpus; clustering
it into proposed miner rules is a separate downstream step.

Why this corpus. Every other miner idea starts by guessing what this team would
merge, and Phase 1 showed that guessing goes badly — `@since` gaps were predicted
at 1-5% and measured at 100% adherence. Review comments are the record of what
maintainers actually spend their scarce attention on, which is the thing worth
learning rather than assuming.

The load-bearing filter is **"merged, but only after changes were requested."**
That isolates comments which demonstrably caused a contributor to change
something — a rule the team enforces that the contributor did not know. Comments
on PRs merged without friction teach nothing, and comments on rejected PRs are
about the idea rather than the craft.

Note the PR listing is sorted by `created`, never `updated`. Every open PR in this
repo reports the same `updated_at` because a bulk label event reset the queue, and
the same hazard applies to closed ones.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Iterator

from ..github import GitHubClient

from ..contract import CostClass
from .base import HarvestDefect

CORPUS = "review_comments"
COST_CLASS = CostClass.A_API
DESCRIPTION = "maintainer review comments that caused a change"
SCHEMA_VERSION = 1

MAINTAINER_ASSOCIATIONS = frozenset({"MEMBER", "OWNER"})

# A comment shorter than this, once quoted text and markdown noise are stripped,
# carries no rule worth extracting.
_MIN_BODY_CHARS = 40

_TRIVIAL = frozenset(
    {
        "lgtm", "thanks", "thank you", "nice", "good catch", "ok", "okay",
        "done", "agreed", "+1", "sgtm", "ship it", "fixed", "sounds good",
    }
)

_RE_QUOTE = re.compile(r"^\s*>.*$", re.MULTILINE)
_RE_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_RE_WS = re.compile(r"\s+")

_MAX_BODY_CHARS = 4000
_MAX_HUNK_CHARS = 300


@dataclass(frozen=True)
class ReviewComment:
    pr: int
    pr_merged_at: str
    comment_id: int
    author: str
    author_association: str
    created_at: str
    path: str
    body: str
    diff_hunk_tail: str
    url: str

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "corpus": CORPUS,
            "pr": self.pr,
            "pr_merged_at": self.pr_merged_at,
            "comment_id": self.comment_id,
            "author": self.author,
            "author_association": self.author_association,
            "created_at": self.created_at,
            "path": self.path,
            "body": self.body,
            "diff_hunk_tail": self.diff_hunk_tail,
            "url": self.url,
            "body_digest": "sha256:"
            + hashlib.sha256(_normalise(self.body).encode("utf-8")).hexdigest(),
        }


@dataclass
class HarvestStats:
    prs_listed: int = 0
    prs_merged: int = 0
    prs_with_changes_requested: int = 0
    comments_seen: int = 0
    comments_kept: int = 0

    def to_json(self) -> dict[str, int]:
        return {
            "prs_listed": self.prs_listed,
            "prs_merged": self.prs_merged,
            "prs_with_changes_requested": self.prs_with_changes_requested,
            "comments_seen": self.comments_seen,
            "comments_kept": self.comments_kept,
        }


def harvest(
    client: GitHubClient, repo: str, *, limit: int = 100
) -> tuple[list[ReviewComment], HarvestStats]:
    stats = HarvestStats()
    kept: list[ReviewComment] = []

    for pull in _iter_closed_prs(client, repo, limit, stats):
        # Paginated, not a single get(): a plain get returns one 30-item page,
        # and a heavily-reviewed PR keeps its CHANGES_REQUESTED review -- and
        # the very comments this harvester exists for -- beyond page one.
        reviews = list(
            client.paginate(f"/repos/{repo}/pulls/{pull['number']}/reviews?per_page=100")
        )
        if not _has_maintainer_changes_requested(reviews):
            continue
        stats.prs_with_changes_requested += 1

        comments = list(
            client.paginate(f"/repos/{repo}/pulls/{pull['number']}/comments?per_page=100")
        )
        author = (pull.get("user") or {}).get("login", "")
        for comment in comments:
            stats.comments_seen += 1
            harvested = _accept(comment, pull, author)
            if harvested is not None:
                kept.append(harvested)
                stats.comments_kept += 1

    _self_check(stats)
    return sorted(kept, key=lambda c: (c.pr, c.comment_id)), stats


def _iter_closed_prs(
    client: GitHubClient, repo: str, limit: int, stats: HarvestStats
) -> Iterator[dict]:
    # Sorted by creation, deliberately: `updated` is unusable in this repo.
    path = f"/repos/{repo}/pulls?state=closed&sort=created&direction=desc&per_page=100"
    for pull in client.paginate(path):
        if stats.prs_merged >= limit:
            return
        stats.prs_listed += 1
        if not pull.get("merged_at"):
            continue
        stats.prs_merged += 1
        yield pull


def _has_maintainer_changes_requested(reviews: Any) -> bool:
    if not isinstance(reviews, list):
        return False
    return any(
        review.get("state") == "CHANGES_REQUESTED"
        and review.get("author_association") in MAINTAINER_ASSOCIATIONS
        and not _is_bot(review.get("user") or {})
        for review in reviews
    )


def _accept(comment: dict, pull: dict, pr_author: str) -> ReviewComment | None:
    user = comment.get("user") or {}
    login = user.get("login", "")

    if comment.get("author_association") not in MAINTAINER_ASSOCIATIONS:
        return None
    if _is_bot(user) or not login or login == pr_author:
        return None

    body = (comment.get("body") or "").strip()
    if not _is_substantive(body):
        return None

    hunk = (comment.get("diff_hunk") or "").strip()
    return ReviewComment(
        pr=pull["number"],
        pr_merged_at=pull.get("merged_at") or "",
        comment_id=comment.get("id", 0),
        author=login,
        author_association=comment.get("author_association", ""),
        created_at=comment.get("created_at") or "",
        path=comment.get("path") or "",
        body=body[:_MAX_BODY_CHARS],
        diff_hunk_tail="\n".join(hunk.splitlines()[-3:])[:_MAX_HUNK_CHARS],
        url=comment.get("html_url") or "",
    )


def _is_bot(user: dict) -> bool:
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


def _normalise(body: str) -> str:
    stripped = _RE_CODE_FENCE.sub(" ", _RE_QUOTE.sub(" ", body))
    return _RE_WS.sub(" ", stripped).strip()


def _is_substantive(body: str) -> bool:
    normalised = _normalise(body)
    # Strip only punctuation and space: a character-set rstrip that includes
    # letters silently mangles words ("okay" -> "oka").
    if normalised.lower().rstrip(" .!") in _TRIVIAL:
        return False
    return len(normalised) >= _MIN_BODY_CHARS


def _self_check(stats: HarvestStats) -> None:
    """Refuse to emit an empty corpus that the filters obviously broke.

    Consistent with the miners: a percentage check needs an absolute companion,
    or a small sample slips through. Here the absolute rule is that finding
    changes-requested PRs but zero surviving comments means a filter is wrong,
    not that maintainers said nothing.
    """
    if stats.prs_with_changes_requested >= 5 and stats.comments_kept == 0:
        raise HarvestDefect(
            f"examined {stats.prs_with_changes_requested} PRs merged after changes "
            f"were requested and kept 0 of {stats.comments_seen} review comments. "
            f"Maintainers do not request changes silently -- a filter "
            f"(association, bot, or length) is almost certainly wrong. Refusing to emit."
        )
    if stats.prs_merged >= 30 and stats.prs_with_changes_requested == 0:
        raise HarvestDefect(
            f"{stats.prs_merged} merged PRs examined, none with a maintainer "
            f"CHANGES_REQUESTED review. That is implausible for this repo; the "
            f"review-state filter is likely wrong. Refusing to emit."
        )
