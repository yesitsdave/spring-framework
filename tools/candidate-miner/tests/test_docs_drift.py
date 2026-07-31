"""docs_drift harvester, driven by throwaway git repositories."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from candidateminer.harvesters.base import HarvestDefect
from candidateminer.harvesters.docs_drift import harvest

_PAGES = "framework-docs/modules/ROOT/pages"
_SRC = "spring-core/src/main/java/org/springframework"

_GIT_ID = [
    "-c", "user.email=t@example.com",
    "-c", "user.name=Test",
    "-c", "commit.gpgsign=false",
]


class _Repo:
    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self._git("init", "-q", "-b", "main")

    def _git(self, *args: str) -> None:
        subprocess.run(
            ["git", *_GIT_ID, *args], cwd=self.root, check=True, capture_output=True
        )

    def write(self, rel: str, body: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def commit(self, message: str, date: str) -> None:
        self._git("add", "-A")
        stamp = f"{date}T12:00:00"
        subprocess.run(
            ["git", *_GIT_ID, "commit", "-q", "-m", message,
             "--date", stamp, "--allow-empty"],
            cwd=self.root, check=True, capture_output=True,
            env={"GIT_COMMITTER_DATE": stamp, "PATH": "/usr/bin:/bin"},
        )


def _klass(members: list[str]) -> str:
    body = "\n".join(f"\t{m}" for m in members)
    return f"package org.springframework;\npublic class Thing {{\n{body}\n}}\n"


def _page(types: list[str]) -> str:
    lines = [f"Uses {t} for things." for t in types]
    return "= Page\n\n" + "\n".join(lines) + "\n"


_THREE_TYPES = [
    "org.springframework.Thing",
    "org.springframework.Other",
    "org.springframework.Third",
]


def _base_repo() -> _Repo:
    repo = _Repo()
    for name in ("Thing", "Other", "Third"):
        repo.write(f"{_SRC}/{name}.java",
                   f"package org.springframework;\npublic class {name} {{\n}}\n")
    repo.write(f"{_PAGES}/sample.adoc", _page(_THREE_TYPES))
    repo.commit("initial", "2024-01-01")
    return repo


class SelectionTests(unittest.TestCase):
    def test_page_with_too_few_references_is_skipped(self):
        repo = _base_repo()
        repo.write(f"{_PAGES}/sample.adoc", _page(["org.springframework.Thing"]))
        repo.commit("thin page", "2024-01-02")
        repo.write(f"{_SRC}/Thing.java", _klass(["public void added() {}"]))
        repo.commit("api change", "2025-01-01")
        found, stats = harvest(repo.root)
        self.assertEqual(found, [])
        self.assertEqual(stats.pages_with_enough_refs, 0)

    def test_unchanged_code_is_not_drift(self):
        found, stats = harvest(_base_repo().root)
        self.assertEqual(found, [])
        self.assertEqual(stats.pages_code_moved_on, 0)

    def test_internal_change_without_a_public_signature_is_not_drift(self):
        """Refactors do not invalidate prose; only API changes might."""
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["private int cache = 1;"]))
        repo.commit("internal tweak", "2025-06-01")
        found, stats = harvest(repo.root)
        self.assertEqual(stats.pages_code_moved_on, 1)
        self.assertEqual(stats.pages_with_signature_change, 0)
        self.assertEqual(found, [])

    def test_public_signature_change_is_drift(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].page, "sample.adoc")
        self.assertGreaterEqual(found[0].signature_change_count, 1)
        self.assertTrue(
            any("freshMethod" in c for c in found[0].signature_changes),
            found[0].signature_changes,
        )


class AnnotationChurnTests(unittest.TestCase):
    """71% of raw signature changes in this repo were annotation-only.

    Six of fourteen pages had nothing else -- their whole signal was the JSpecify
    migration, a mechanical sweep that cannot make prose wrong.
    """

    def _repo_with(self, before: str, after: str) -> _Repo:
        repo = _Repo()
        for name in ("Other", "Third"):
            repo.write(f"{_SRC}/{name}.java",
                       f"package org.springframework;\npublic class {name} {{\n}}\n")
        repo.write(f"{_SRC}/Thing.java", _klass([before]))
        repo.write(f"{_PAGES}/sample.adoc", _page(_THREE_TYPES))
        repo.commit("initial", "2024-01-01")
        repo.write(f"{_SRC}/Thing.java", _klass([after]))
        repo.commit("change", "2025-06-01")
        return repo

    def test_adding_nullable_is_not_drift(self):
        repo = self._repo_with(
            "public void handle(String value) {}",
            "public void handle(@Nullable String value) {}",
        )
        found, stats = harvest(repo.root)
        self.assertEqual(stats.pages_code_moved_on, 1)
        self.assertEqual(found, [], "JSpecify churn must not read as drift")

    def test_adding_override_is_not_drift(self):
        repo = self._repo_with(
            "public void handle(String value) {}",
            "@Override public void handle(String value) {}",
        )
        self.assertEqual(harvest(repo.root)[0], [])

    def test_a_real_parameter_change_still_counts(self):
        repo = self._repo_with(
            "public void handle(String value) {}",
            "public void handle(@Nullable String value, int extra) {}",
        )
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1)

    def test_cancellation_is_by_multiset(self):
        """One annotation edit must not swallow a genuine repeated change."""
        repo = self._repo_with(
            "public void handle(String value) {}",
            "public void handle(@Nullable String value) {}",
        )
        repo.write(f"{_SRC}/Thing.java", _klass([
            "public void handle(@Nullable String value) {}",
            "public void brandNew() {}",
        ]))
        repo.commit("add method", "2025-07-01")
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1)
        self.assertTrue(any("brandNew" in c for c in found[0].signature_changes))
        self.assertFalse(any("@Nullable" in c for c in found[0].signature_changes))


class BulkCommitTests(unittest.TestCase):
    """The trap that made 22 pages all share one date."""

    def test_a_bulk_docs_sweep_does_not_count_as_the_page_changing(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        # A repo-wide sweep that also touches the page. Without bulk exclusion
        # the page would look newer than the code and drop out of the results.
        for i in range(60):
            repo.write(f"noise/file{i}.txt", "x")
        repo.write(f"{_PAGES}/sample.adoc", _page(_THREE_TYPES) + "\n// swept\n")
        repo.commit("Update copyright headers", "2026-01-01")
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1, "bulk sweep should not mask the drift")
        self.assertEqual(found[0].doc_last_date, "2024-01-01")

    def test_a_normal_docs_edit_does_clear_the_drift(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        repo.write(f"{_PAGES}/sample.adoc", _page(_THREE_TYPES) + "\nRevised.\n")
        repo.commit("document freshMethod", "2026-01-01")
        self.assertEqual(harvest(repo.root)[0], [])


class HistoryWindowTests(unittest.TestCase):
    """Pages last edited before _HISTORY_SINCE (2022) must not vanish.

    The regression: the commit-date index only covered the window, so a page
    whose last substantive edit predated it resolved to no date and was
    silently dropped -- excluding exactly the longest-untouched, highest-drift
    pages the harvester exists to find.
    """

    def test_page_untouched_since_before_the_window_is_still_reported(self):
        repo = _Repo()
        for name in ("Thing", "Other", "Third"):
            repo.write(f"{_SRC}/{name}.java",
                       f"package org.springframework;\npublic class {name} {{\n}}\n")
        repo.write(f"{_PAGES}/ancient.adoc", _page(_THREE_TYPES))
        repo.commit("initial", "2021-06-01")
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].doc_last_date, "2021-06-01")

    def test_a_pre_window_bulk_sweep_is_still_excluded(self):
        repo = _Repo()
        for name in ("Thing", "Other", "Third"):
            repo.write(f"{_SRC}/{name}.java",
                       f"package org.springframework;\npublic class {name} {{\n}}\n")
        repo.write(f"{_PAGES}/ancient.adoc", _page(_THREE_TYPES))
        repo.commit("initial", "2021-03-01")
        for i in range(60):
            repo.write(f"noise/file{i}.txt", "x")
        repo.write(f"{_PAGES}/ancient.adoc", _page(_THREE_TYPES) + "\n// swept\n")
        repo.commit("Update copyright headers", "2021-12-01")
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        found, _ = harvest(repo.root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].doc_last_date, "2021-03-01",
                         "the pre-window sweep must not count as the page changing")


class OrderingTests(unittest.TestCase):
    def test_pages_are_ranked_by_signature_churn(self):
        repo = _base_repo()
        repo.write(f"{_PAGES}/quiet.adoc", _page(["org.springframework.Other",
                                                  "org.springframework.Third",
                                                  "org.springframework.Thing"]))
        repo.commit("second page", "2024-01-02")
        repo.write(f"{_SRC}/Thing.java",
                   _klass([f"public void m{i}() {{}}" for i in range(5)]))
        repo.commit("lots of api", "2025-06-01")
        found, _ = harvest(repo.root)
        counts = [p.signature_change_count for p in found]
        self.assertEqual(counts, sorted(counts, reverse=True))

    def test_is_deterministic(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        first = [p.to_record() for p in harvest(repo.root)[0]]
        second = [p.to_record() for p in harvest(repo.root)[0]]
        self.assertEqual(first, second)

    def test_record_carries_no_wallclock(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        record = harvest(repo.root)[0][0].to_record()
        self.assertNotIn("harvested_at", record)
        self.assertEqual(record["corpus"], "docs_drift")


class SelfCheckTests(unittest.TestCase):
    def test_flagging_most_pages_is_a_defect(self):
        """A pre-filter that keeps nearly everything is not a pre-filter."""
        repo = _base_repo()
        for i in range(25):
            repo.write(f"{_PAGES}/p{i}.adoc", _page(_THREE_TYPES))
        repo.commit("many pages", "2024-01-02")
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        with self.assertRaises(HarvestDefect) as caught:
            harvest(repo.root)
        self.assertIn("narrow reading list", str(caught.exception))

    def test_small_corpus_does_not_trip_the_check(self):
        repo = _base_repo()
        repo.write(f"{_SRC}/Thing.java", _klass(["public void freshMethod() {}"]))
        repo.commit("add public API", "2025-06-01")
        self.assertEqual(len(harvest(repo.root)[0]), 1)


if __name__ == "__main__":
    unittest.main()
