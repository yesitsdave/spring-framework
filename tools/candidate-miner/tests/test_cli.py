"""CLI plumbing, against a tiny fixture repo rather than the real checkout."""

from __future__ import annotations

import contextlib
import json
import io
import tempfile
import unittest
from pathlib import Path

import miner


def _fixture_repo() -> Path:
    root = Path(tempfile.mkdtemp())
    src = root / "spring-core/src/main/java/org/springframework/util"
    src.mkdir(parents=True)
    (src / "Alive.java").write_text("package org.springframework.util;", encoding="utf-8")
    docs = root / "framework-docs/modules/ROOT/pages"
    docs.mkdir(parents=True)
    (docs / "p.adoc").write_text(
        '<bean class="org.springframework.gone.Missing"/>\n', encoding="utf-8"
    )
    return root


class _Run:
    """Invokes the CLI in-process, with ledger and output redirected to a temp dir.

    `isolated=False` is for subcommands like `list` that take neither path flag.
    """

    def __init__(self, *argv: str, isolated: bool = True) -> None:
        self.workspace = Path(tempfile.mkdtemp())
        self.argv = list(argv)
        self.isolated = isolated

    def __call__(self, *extra: str):
        argv = list(self.argv)
        if self.isolated:
            argv += [
                "--ledger-dir", str(self.workspace / "ledger"),
                "--out-dir", str(self.workspace / "out"),
            ]
        argv += list(extra)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = miner.main(argv)
        return code, out.getvalue(), err.getvalue()


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.repo = _fixture_repo()
        self.run = _Run("run", "--miner", "docs_dead_ref", "--repo", str(self.repo), "--dry-run")

    def _jsonl_lines(self, text: str) -> list[str]:
        return [l for l in text.splitlines() if l.startswith("{")]

    def test_jsonl_writes_records_to_stdout(self):
        code, out, _ = self.run("--format", "jsonl")
        self.assertEqual(code, 0)
        self.assertEqual(len(self._jsonl_lines(out)), 1)

    def test_summary_writes_no_records(self):
        _, out, _ = self.run("--format", "summary")
        self.assertEqual(self._jsonl_lines(out), [])
        self.assertIn("candidate(s) to review", out)

    def test_both_really_means_both(self):
        """`both` used to print only the summary -- the flag lied."""
        _, out, _ = self.run("--format", "both")
        self.assertEqual(len(self._jsonl_lines(out)), 1)
        self.assertIn("candidate(s) to review", out)


class ExitCodeTests(unittest.TestCase):
    def test_unknown_miner_is_an_error(self):
        code, _, err = _Run("run", "--miner", "nope")()
        self.assertEqual(code, miner.EXIT_ERROR)
        self.assertIn("unknown miner", err)

    def test_missing_repo_is_an_error(self):
        code, _, err = _Run("run", "--miner", "docs_dead_ref", "--repo", "/nonexistent")()
        self.assertEqual(code, miner.EXIT_ERROR)
        self.assertIn("does not exist", err)

    def test_miner_defect_exits_two(self):
        repo = _fixture_repo()
        page = repo / "framework-docs/modules/ROOT/pages/p.adoc"
        page.write_text(
            "".join(f'<bean class="org.springframework.gone.M{i}"/>\n' for i in range(30)),
            encoding="utf-8",
        )
        code, _, err = _Run(
            "run", "--miner", "docs_dead_ref", "--repo", str(repo), "--dry-run"
        )()
        self.assertEqual(code, miner.EXIT_DEFECT)
        self.assertIn("DEFECT", err)

    def test_list_succeeds(self):
        code, out, _ = _Run("list", isolated=False)()
        self.assertEqual(code, 0)
        self.assertIn("docs_dead_ref", out)
        self.assertIn("config_dead_entry", out)


class MultiLedgerTests(unittest.TestCase):
    """`--miner` used to default to one name, hiding every other ledger."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.ledger_dir = self.workspace / "ledger"
        self.ledger_dir.mkdir()
        self.fingerprints = {}
        for name, identity in (
            ("docs_dead_ref", "org.springframework.gone.A"),
            ("config_dead_entry", "checkstyle_suppression:Gone_B"),
        ):
            fingerprint = f"sha256:{abs(hash(name)):064x}"[:71]
            self.fingerprints[name] = fingerprint
            record = {
                "fingerprint": fingerprint,
                "state": "new",
                "category": "x",
                "path": "p",
                "identity": identity,
                "first_seen_commit": "c",
                "last_seen_commit": "c",
            }
            (self.ledger_dir / f"{name}.jsonl").write_text(
                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = miner.main([*argv, "--ledger-dir", str(self.ledger_dir)])
        return code, out.getvalue(), err.getvalue()

    def test_show_without_miner_lists_every_ledger(self):
        code, out, _ = self._cli("ledger", "show")
        self.assertEqual(code, 0)
        self.assertIn("ledger: docs_dead_ref", out)
        self.assertIn("ledger: config_dead_entry", out)

    def test_state_change_finds_the_right_ledger(self):
        """Declining used to fail with 'unknown fingerprint' for a real entry."""
        code, out, _ = self._cli(
            "ledger", "decline", self.fingerprints["config_dead_entry"],
            "--reason", "not worth it",
        )
        self.assertEqual(code, 0)
        self.assertIn("declined", out)
        body = (self.ledger_dir / "config_dead_entry.jsonl").read_text()
        self.assertIn('"state":"declined"', body)
        self.assertNotIn(
            '"state":"declined"', (self.ledger_dir / "docs_dead_ref.jsonl").read_text()
        )

    def test_unknown_fingerprint_names_the_ledgers_searched(self):
        code, _, err = self._cli("ledger", "queue", "sha256:" + "0" * 64)
        self.assertEqual(code, miner.EXIT_ERROR)
        self.assertIn("not found in any ledger", err)
        self.assertIn("config_dead_entry", err)


class CorpusTests(unittest.TestCase):
    """Harvesters emit reading material; it needs a viewer like the ledger has."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.out = self.workspace / "out"
        self.out.mkdir()
        record = {
            "schema_version": 1,
            "corpus": "docs_drift",
            "page": "core/beans/x.adoc",
            "path": "framework-docs/modules/ROOT/pages/core/beans/x.adoc",
            "doc_last_substantive": {"commit": "abc", "date": "2024-01-01"},
            "code_last_substantive": {"date": "2026-01-01"},
            "referenced_types": ["org.springframework.Thing"],
            "referenced_type_count": 4,
            "signature_change_count": 2,
            "signature_changes": ["+ public void added()", "- public void gone()"],
            "files_examined": 4,
            "files_truncated": False,
        }
        (self.out / "docs_drift.corpus.jsonl").write_text(
            json.dumps(record) + "\n", encoding="utf-8"
        )

    def _cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = miner.main([*argv, "--out-dir", str(self.out)])
        return code, out.getvalue(), err.getvalue()

    def test_list_reports_harvested_and_missing(self):
        code, out, _ = self._cli("corpus", "list")
        self.assertEqual(code, 0)
        self.assertIn("docs_drift", out)
        self.assertIn("1 records", out)
        self.assertIn("not harvested yet", out)

    def test_show_renders_records(self):
        code, out, _ = self._cli("corpus", "show", "--source", "docs_drift")
        self.assertEqual(code, 0)
        self.assertIn("core/beans/x.adoc", out)
        self.assertIn("2024-01-01", out)
        self.assertIn("public void added()", out)

    def test_limit_truncates_and_says_so(self):
        path = self.out / "docs_drift.corpus.jsonl"
        base = json.loads(path.read_text())
        path.write_text(
            "".join(json.dumps({**base, "page": f"p{i}.adoc"}) + "\n" for i in range(5)),
            encoding="utf-8",
        )
        _, out, _ = self._cli("corpus", "show", "--source", "docs_drift", "--limit", "2")
        self.assertIn("3 more", out)

    def test_unharvested_corpus_says_how_to_get_it(self):
        code, _, err = self._cli("corpus", "show", "--source", "review_comments")
        self.assertEqual(code, miner.EXIT_ERROR)
        self.assertIn("harvest --source review_comments", err)

    def test_unknown_corpus_is_a_different_error(self):
        """Must not tell the user to harvest something that does not exist."""
        code, _, err = self._cli("corpus", "show", "--source", "nope")
        self.assertEqual(code, miner.EXIT_ERROR)
        self.assertIn("unknown corpus", err)
        self.assertNotIn("harvest --source nope", err)


class DryRunTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        repo = _fixture_repo()
        runner = _Run("run", "--miner", "docs_dead_ref", "--repo", str(repo), "--dry-run")
        runner()
        self.assertFalse((runner.workspace / "ledger").exists())
        self.assertFalse((runner.workspace / "out").exists())

    def test_real_run_writes_ledger_and_output(self):
        repo = _fixture_repo()
        runner = _Run("run", "--miner", "docs_dead_ref", "--repo", str(repo))
        code, _, _ = runner()
        self.assertEqual(code, 0)
        self.assertTrue((runner.workspace / "ledger/docs_dead_ref.jsonl").is_file())
        self.assertTrue((runner.workspace / "out/docs_dead_ref.candidates.jsonl").is_file())


if __name__ == "__main__":
    unittest.main()
