"""The docs_dead_ref miner, driven entirely by synthetic fixtures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidateminer.miners import MinerContext, MinerDefect
from candidateminer.miners.docs_dead_ref import MINER

_REAL = "spring-context/src/main/java/org/springframework/context/support/Thing.java"
_FIXTURE = "spring-beans/src/testFixtures/java/org/springframework/beans/testfixture/Bean.java"


def _tree(adoc: str, sources: dict[str, str] | None = None) -> Path:
    root = Path(tempfile.mkdtemp())
    sources = {_REAL: "", _FIXTURE: ""} if sources is None else sources
    for rel, body in sources.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    docs = root / "framework-docs" / "modules" / "ROOT" / "pages"
    docs.mkdir(parents=True)
    (docs / "sample.adoc").write_text(adoc, encoding="utf-8")
    return root


def _run(root: Path, **options):
    return list(MINER.run(MinerContext(root=root, options=options)))


class ExtractorTests(unittest.TestCase):
    def test_flags_dead_xml_bean_class(self):
        root = _tree('<bean class="org.springframework.gone.Missing"/>\n')
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].identity, "org.springframework.gone.Missing")
        self.assertEqual(found[0].evidence["primary_extractor"], "xml_class_attr")

    def test_ignores_live_xml_bean_class(self):
        root = _tree('<bean class="org.springframework.context.support.Thing"/>\n')
        self.assertEqual(_run(root), [])

    def test_flags_dead_javadoc_url(self):
        root = _tree("see {spring-framework-api}/gone/Missing.html[docs]\n")
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].identity, "org.springframework.gone.Missing")
        self.assertEqual(found[0].evidence["primary_extractor"], "api_url")

    def test_javadoc_url_base_already_contains_org_springframework(self):
        """The recon bug: forgetting the prefix reported every link as dead."""
        root = _tree("{spring-framework-api}/context/support/Thing.html[ok]\n")
        self.assertEqual(_run(root), [])

    def test_prose_extractor_is_off_by_default(self):
        root = _tree("Configure org.springframework.gone.Missing as needed.\n")
        self.assertEqual(_run(root), [])
        self.assertEqual(len(_run(root, include_experimental=True)), 1)

    def test_denylisted_projects_are_ignored(self):
        root = _tree(
            '<bean class="org.springframework.boot.Missing"/>\n'
            '<bean class="org.springframework.samples.MyService"/>\n'
        )
        self.assertEqual(_run(root), [])

    def test_nested_type_resolves_to_its_enclosing_file(self):
        root = _tree('<bean class="org.springframework.context.support.Thing.Builder"/>\n')
        self.assertEqual(_run(root), [])


class GroupingTests(unittest.TestCase):
    def test_repeated_reference_in_one_file_is_a_single_candidate(self):
        """Same dead FQN, same file, same fix -- one record, not three."""
        root = _tree(
            '<bean class="org.springframework.gone.Missing"/>\n' * 3
        )
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertEqual(len(found[0].evidence["occurrences"]), 3)

    def test_fingerprint_is_unaffected_by_occurrence_count(self):
        one = _run(_tree('<bean class="org.springframework.gone.Missing"/>\n'))[0]
        many = _run(_tree('<bean class="org.springframework.gone.Missing"/>\n' * 4))[0]
        self.assertEqual(one.fingerprint, many.fingerprint)

    def test_output_is_sorted_by_fingerprint(self):
        root = _tree(
            "".join(
                f'<bean class="org.springframework.gone.Missing{i}"/>\n' for i in range(6)
            )
        )
        found = _run(root)
        self.assertEqual([c.fingerprint for c in found], sorted(c.fingerprint for c in found))


class RelocationConfidenceTests(unittest.TestCase):
    def test_unique_public_match_is_unambiguous(self):
        root = _tree('<bean class="org.springframework.wrong.Thing"/>\n')
        self.assertEqual(_run(root)[0].evidence["relocation_confidence"], "unambiguous")

    def test_test_only_match_is_not_a_usable_replacement(self):
        root = _tree('<bean class="org.springframework.wrong.Bean"/>\n')
        found = _run(root)[0]
        self.assertEqual(found.evidence["relocation_confidence"], "test_only")
        self.assertTrue(found.evidence["illustrative_only"])

    def test_common_name_with_many_matches_is_ambiguous(self):
        """Regression: `TestBean` exists five times; matching one invents a move."""
        sources = {
            "a/src/main/java/org/springframework/one/TestBean.java": "",
            "b/src/test/java/org/springframework/two/TestBean.java": "",
            "c/src/test/java/org/springframework/three/TestBean.java": "",
        }
        root = _tree('<bean class="org.springframework.beans.TestBean"/>\n', sources)
        found = _run(root)[0]
        self.assertEqual(found.evidence["relocation_confidence"], "ambiguous")

    def test_deleted_outright_has_no_relocation(self):
        root = _tree('<bean class="org.springframework.gone.Missing"/>\n')
        self.assertEqual(_run(root)[0].evidence["relocation_confidence"], "none")

    def test_unambiguous_outranks_ambiguous(self):
        unique = _run(_tree('<bean class="org.springframework.wrong.Thing"/>\n'))[0]
        illustrative = _run(_tree('<bean class="org.springframework.wrong.Bean"/>\n'))[0]
        self.assertGreater(unique.score.value, illustrative.score.value)


class SelfCheckTests(unittest.TestCase):
    """The tool must notice when it is broken rather than emit a wall of findings."""

    def test_implausible_miss_rate_raises_a_defect(self):
        adoc = "".join(
            f'<bean class="org.springframework.gone.Missing{i}"/>\n' for i in range(30)
        )
        with self.assertRaises(MinerDefect) as caught:
            _run(_tree(adoc))
        self.assertIn("sanity ceiling", str(caught.exception))

    def test_small_samples_do_not_trip_the_check(self):
        """Below the minimum sample a high rate is not evidence of a bug."""
        adoc = "".join(
            f'<bean class="org.springframework.gone.Missing{i}"/>\n' for i in range(3)
        )
        self.assertEqual(len(_run(_tree(adoc))), 3)

    def test_zero_extraction_across_a_large_docs_tree_is_a_defect(self):
        """The mirror-image blind spot: every ceiling guards against too many
        findings, but a broken extractor that matches nothing would sail through
        with an empty result that reads as a clean bill of health."""
        root = _tree("No references here.\n")
        docs = root / "framework-docs" / "modules" / "ROOT" / "pages"
        for i in range(60):
            (docs / f"page{i}.adoc").write_text("Nothing to extract.\n", encoding="utf-8")
        with self.assertRaises(MinerDefect) as caught:
            _run(root)
        self.assertIn("zero Spring API references", str(caught.exception))

    def test_a_small_refless_tree_is_legitimately_empty(self):
        """A handful of pages without references is plausible; only scale makes
        zero extraction implausible."""
        self.assertEqual(_run(_tree("No references here.\n")), [])

    def test_ceiling_can_be_raised_deliberately(self):
        adoc = "".join(
            f'<bean class="org.springframework.gone.Missing{i}"/>\n' for i in range(30)
        )
        self.assertEqual(len(_run(_tree(adoc), sanity_ceiling=1.1)), 30)


class DeterminismTests(unittest.TestCase):
    def test_two_runs_produce_identical_bytes(self):
        root = _tree(
            '<bean class="org.springframework.gone.Missing"/>\n'
            "{spring-framework-api}/gone/Other.html[x]\n"
        )
        first = [c.to_jsonl() for c in _run(root)]
        second = [c.to_jsonl() for c in _run(root)]
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
