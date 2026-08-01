"""solve_target.py: the deterministic brackets around the solve agent.

Same testing posture as test_scan.py: real miner CLI against a fixture repo,
no network -- the fake client serves the issue body.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import scan
import solve_target
from candidateminer.contract import canonical_json


def _fingerprint(char: str) -> str:
    return "sha256:" + char * 64


class FakeClient:
    def __init__(self, body: str | None):
        self.body = body

    def get(self, path: str):
        return {"body": self.body}


def _fixture_repo(dead_reference: bool = True) -> Path:
    root = Path(tempfile.mkdtemp())
    src = root / "spring-core/src/main/java/org/springframework/util"
    src.mkdir(parents=True)
    (src / "Alive.java").write_text("package org.springframework.util;", encoding="utf-8")
    docs = root / "framework-docs/modules/ROOT/pages"
    docs.mkdir(parents=True)
    reference = "org.springframework.gone.Missing" if dead_reference else "org.springframework.util.Alive"
    (docs / "p.adoc").write_text(f'<bean class="{reference}"/>\n', encoding="utf-8")
    return root


class SolveTargetTests(unittest.TestCase):
    def setUp(self):
        self.repo = _fixture_repo()
        self.workspace = Path(tempfile.mkdtemp())
        self.out_dir = self.workspace / "out"
        self.ledger_dir = self.workspace / "ledger"
        self.ledger_dir.mkdir(parents=True)

    def _mine_fingerprint(self) -> str:
        """Discover the fixture candidate's real fingerprint via the miner."""
        code, out, _ = scan.run_miner_cli(
            ["run", "--miner", "docs_dead_ref", "--dry-run", "--format", "jsonl",
             "--repo", str(self.repo), "--ledger-dir", str(self.ledger_dir),
             "--out-dir", str(self.out_dir)]
        )
        assert code == 0
        return scan.parse_candidates(out)[0]["fingerprint"]

    def _write_ledger(self, fingerprint: str, *, stem: str = "docs_dead_ref", state: str = "new"):
        entry = {
            "fingerprint": fingerprint, "state": state,
            "category": "docs.dead_api_reference",
            "path": "framework-docs/modules/ROOT/pages/p.adoc",
            "identity": "org.springframework.gone.Missing",
            "first_seen_commit": "abc", "last_seen_commit": "abc",
        }
        if state == "declined":
            entry["reason"] = "test veto"
        (self.ledger_dir / f"{stem}.jsonl").write_text(
            canonical_json(entry) + "\n", encoding="utf-8"
        )

    def _run(self, body: str | None) -> dict:
        with contextlib.redirect_stdout(io.StringIO()):
            code = solve_target.main(
                ["--repo-slug", "o/r", "--issue", "7",
                 "--repo", str(self.repo),
                 "--ledger-dir", str(self.ledger_dir),
                 "--out-dir", str(self.out_dir)],
                runner=scan.run_miner_cli,
                client=FakeClient(body),
            )
        self.assertEqual(code, solve_target.EXIT_OK)
        return json.loads((self.out_dir / "solve_target.json").read_text(encoding="utf-8"))

    def test_confirmed_carries_the_candidate_record(self):
        fp = self._mine_fingerprint()
        self._write_ledger(fp)
        result = self._run(f"body\n`candidate-miner:fingerprint {fp}`\n")
        self.assertEqual(result["verdict"], "confirmed")
        self.assertEqual(result["miner"], "docs_dead_ref")
        self.assertEqual(result["candidate"]["fingerprint"], fp)

    def test_gone_after_the_reference_is_fixed(self):
        fp = self._mine_fingerprint()
        self._write_ledger(fp)
        page = self.repo / "framework-docs/modules/ROOT/pages/p.adoc"
        page.write_text('<bean class="org.springframework.util.Alive"/>\n', encoding="utf-8")
        result = self._run(f"`candidate-miner:fingerprint {fp}`")
        self.assertEqual(result["verdict"], "gone")
        self.assertNotIn("candidate", result)

    def test_no_marker(self):
        self.assertEqual(self._run("a body without any marker")["verdict"], "no_marker")

    def test_ambiguous_marker(self):
        body = (f"`candidate-miner:fingerprint {_fingerprint('a')}`\n"
                f"`candidate-miner:fingerprint {_fingerprint('b')}`")
        self.assertEqual(self._run(body)["verdict"], "ambiguous_marker")

    def test_unknown_fingerprint(self):
        result = self._run(f"`candidate-miner:fingerprint {_fingerprint('d')}`")
        self.assertEqual(result["verdict"], "unknown_fingerprint")

    def test_declined_is_a_veto(self):
        fp = self._mine_fingerprint()
        self._write_ledger(fp, state="declined")
        result = self._run(f"`candidate-miner:fingerprint {fp}`")
        self.assertEqual(result["verdict"], "declined")
        self.assertEqual(result["reason"], "test veto")

    def test_corpus_miner_is_unsupported(self):
        fp = _fingerprint("e")
        self._write_ledger(fp, stem="flaky_test")
        result = self._run(f"`candidate-miner:fingerprint {fp}`")
        self.assertEqual(result["verdict"], "unsupported")

    def test_adjudicated_ledger_is_unsupported(self):
        fp = _fingerprint("f")
        self._write_ledger(fp, stem="adjudicated_docs_drift")
        result = self._run(f"`candidate-miner:fingerprint {fp}`")
        self.assertEqual(result["verdict"], "unsupported")

    def test_fingerprint_mode_needs_no_client(self):
        """The offline post-fix check: --fingerprint, no issue fetch."""
        fp = self._mine_fingerprint()
        self._write_ledger(fp)
        with contextlib.redirect_stdout(io.StringIO()):
            code = solve_target.main(
                ["--repo-slug", "o/r", "--fingerprint", fp,
                 "--repo", str(self.repo),
                 "--ledger-dir", str(self.ledger_dir),
                 "--out-dir", str(self.out_dir)],
                runner=scan.run_miner_cli,
                client=None,
            )
        self.assertEqual(code, solve_target.EXIT_OK)
        result = json.loads((self.out_dir / "solve_target.json").read_text(encoding="utf-8"))
        self.assertEqual(result["verdict"], "confirmed")

    def test_miner_defect_propagates_exit_2(self):
        fp = self._mine_fingerprint()
        self._write_ledger(fp)

        def defective(argv):
            return 2, "", "DEFECT: broken\n"

        with contextlib.redirect_stdout(io.StringIO()):
            code = solve_target.main(
                ["--repo-slug", "o/r", "--issue", "7",
                 "--repo", str(self.repo),
                 "--ledger-dir", str(self.ledger_dir),
                 "--out-dir", str(self.out_dir)],
                runner=defective,
                client=FakeClient(f"`candidate-miner:fingerprint {fp}`"),
            )
        self.assertEqual(code, solve_target.EXIT_DEFECT)


if __name__ == "__main__":
    unittest.main()
