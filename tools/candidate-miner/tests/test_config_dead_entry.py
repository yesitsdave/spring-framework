"""The config_dead_entry miner, driven entirely by synthetic fixtures."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidateminer.miners import MinerContext, MinerDefect
from candidateminer.miners.config_dead_entry import MINER

_SUPPRESSIONS = "src/checkstyle/checkstyle-suppressions.xml"
_ALLOWLIST = "src/nohttp/allowlist.lines"


_ANTORA = "framework-docs/antora.yml"


def _antora_yml(attributes: dict[str, str], ext: dict[str, str] | None = None) -> str:
    body = ["name: framework", "version: true"]
    if ext:
        body.append("ext:")
        body.append("  collector:")
        for key, value in ext.items():
            body.append(f"    {key}: {value}")
    body.append("asciidoc:")
    body.append("  attributes:")
    for key, value in attributes.items():
        body.append(f"    {key}: {value}")
    return "\n".join(body) + "\n"


def _tree(
    *,
    suppressions: list[str] | None = None,
    allowlist: list[str] | None = None,
    sources: dict[str, str] | None = None,
) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel, body in (sources or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    if suppressions is not None:
        path = root / _SUPPRESSIONS
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "<suppressions>\n" + "\n".join(
            f'\t<suppress files="{p}" checks="Whatever"/>' for p in suppressions
        ) + "\n</suppressions>\n"
        path.write_text(body, encoding="utf-8")
    if allowlist is not None:
        path = root / _ALLOWLIST
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(allowlist) + "\n", encoding="utf-8")
    return root


def _run(root: Path, **options):
    return list(MINER.run(MinerContext(root=root, options=options)))


_LIVE_SOURCE = {"mod/src/main/java/org/springframework/util/Alive.java": "class Alive {}"}


class SuppressionTests(unittest.TestCase):
    def test_flags_a_suppression_naming_a_deleted_class(self):
        root = _tree(suppressions=["Gone_ClassFinder"], sources=_LIVE_SOURCE)
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].identity, "checkstyle_suppression:Gone_ClassFinder")
        self.assertTrue(found[0].evidence["names_a_literal"])

    def test_ignores_a_suppression_that_still_matches(self):
        root = _tree(suppressions=["Alive"], sources=_LIVE_SOURCE)
        self.assertEqual(_run(root), [])

    def test_build_output_patterns_are_never_flagged(self):
        """Regression: generated sources legitimately do not exist in a clean tree."""
        root = _tree(
            suppressions=[r"[\\/]build[\\/]generated[\\/]sources[\\/]"],
            sources=_LIVE_SOURCE,
        )
        self.assertEqual(_run(root), [])

    def test_path_regex_scores_lower_than_a_literal_name(self):
        literal = _run(_tree(suppressions=["Gone_Thing"], sources=_LIVE_SOURCE))[0]
        pattern = _run(
            _tree(suppressions=[r"[\\/]src[\\/]nowhere[\\/]"], sources=_LIVE_SOURCE)
        )[0]
        self.assertGreater(literal.score.value, pattern.score.value)
        self.assertFalse(pattern.evidence["names_a_literal"])

    def test_unparseable_pattern_is_not_claimed_dead(self):
        root = _tree(suppressions=["(unclosed["], sources=_LIVE_SOURCE)
        self.assertEqual(_run(root), [])

    def test_java_character_classes_are_used_verbatim(self):
        """`[\\\\/]` means the same in both engines; translating it would invite bugs."""
        root = _tree(
            suppressions=[r"[\\/]src[\\/]main[\\/]"],
            sources=_LIVE_SOURCE,
        )
        self.assertEqual(_run(root), [])


class ParserExtractionTests(unittest.TestCase):
    """Zero-extraction guard: a config file that visibly contains entries but
    parses to none means the parser broke, and an empty result would read as a
    clean bill of health."""

    def test_suppressions_that_parse_to_nothing_is_a_defect(self):
        root = _tree(sources=_LIVE_SOURCE)
        path = root / _SUPPRESSIONS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '<suppressions>\n\t<suppress id="x" checks="Y"/>\n</suppressions>\n',
            encoding="utf-8",
        )
        with self.assertRaises(MinerDefect) as caught:
            _run(root)
        self.assertIn("zero entries", str(caught.exception))

    def test_attributes_block_that_parses_to_nothing_is_a_defect(self):
        """A 3-space indent silently defeats the 4-space parser -- fail loudly."""
        yml = "asciidoc:\n  attributes:\n   odd-indent: 'https://example.com'\n"
        root = _tree(sources={**_LIVE_SOURCE, _ANTORA: yml})
        with self.assertRaises(MinerDefect) as caught:
            _run(root)
        self.assertIn("zero entries", str(caught.exception))

    def test_antora_without_an_attributes_block_is_legitimately_silent(self):
        root = _tree(sources={**_LIVE_SOURCE, _ANTORA: "name: framework\nversion: true\n"})
        self.assertEqual(_run(root), [])


class AllowlistTests(unittest.TestCase):
    """nohttp tests allowlist patterns against extracted URLs, not raw file text."""

    def test_anchored_pattern_matches_a_url_sitting_mid_line(self):
        root = _tree(
            allowlist=["^http://live.example.com.*"],
            sources={"docs/x.adoc": 'see the http://live.example.com/page site\n'},
        )
        self.assertEqual(_run(root), [])

    def test_flags_an_allowlist_entry_with_no_matching_url(self):
        root = _tree(
            allowlist=["^http://gone.example.com.*", "^http://live.example.com.*"],
            sources={"docs/x.adoc": "see http://live.example.com/page\n"},
        )
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertIn("gone.example.com", found[0].identity)

    def test_only_text_files_are_scanned(self):
        root = _tree(
            allowlist=["^http://live.example.com.*", "^http://other.example.com.*"],
            sources={
                "docs/x.adoc": "http://live.example.com/a\n",
                "docs/y.bin": "http://other.example.com/b\n",
            },
        )
        found = _run(root)
        self.assertEqual(len(found), 1)
        self.assertIn("other.example.com", found[0].identity)


class SelfCheckTests(unittest.TestCase):
    def test_wholly_dead_config_is_implausible_at_any_size(self):
        """Caught a real bug: wrong URL semantics condemned all six entries."""
        root = _tree(
            allowlist=[f"^http://gone{i}.example.com.*" for i in range(4)],
            sources=_LIVE_SOURCE,
        )
        with self.assertRaises(MinerDefect) as caught:
            _run(root)
        self.assertIn("wholly dead", str(caught.exception))

    def test_two_entries_wholly_dead_is_below_the_floor(self):
        """Deliberate: with only a couple of entries, all-dead is plausible."""
        root = _tree(suppressions=["Gone_A", "Gone_B"], sources=_LIVE_SOURCE)
        self.assertEqual(len(_run(root)), 2)

    def test_high_rate_over_a_decent_sample_raises(self):
        patterns = [f"Gone_{i}" for i in range(10)] + [f"Alive{i}" for i in range(14)]
        sources = {
            f"mod/src/main/java/org/springframework/util/Alive{i}.java": ""
            for i in range(14)
        }
        with self.assertRaises(MinerDefect) as caught:
            _run(_tree(suppressions=patterns, sources=sources))
        self.assertIn("sanity ceiling", str(caught.exception))


class AntoraAttributeTests(unittest.TestCase):
    """Precedent for this cleanup is in the review corpus: PR #31619."""

    def _run_with(self, attributes, docs="", ext=None):
        root = _tree(sources={**_LIVE_SOURCE, "docs/x.adoc": docs})
        path = root / _ANTORA
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_antora_yml(attributes, ext), encoding="utf-8")
        return _run(root)

    def test_flags_an_unreferenced_link_attribute(self):
        found = self._run_with(
            {"used-site": "'https://a.example'", "dead-site": "'https://b.example'"},
            docs="see {used-site} for details\n",
        )
        self.assertEqual([c.identity for c in found], ["antora_attribute:dead-site"])

    def test_attribute_referenced_only_from_antora_yml_counts_as_used(self):
        """Attributes legitimately interpolate one another."""
        found = self._run_with(
            {"base": "'https://a.example'", "issues": "'{base}/issues'"},
            docs="see {issues}\n",
        )
        self.assertEqual(found, [])

    def test_ignores_non_link_settings(self):
        """Replaces a denylist of Asciidoctor built-ins with a scoping rule."""
        found = self._run_with(
            {
                "chomp": "'all'",
                "fold": "'all'",
                "table-stripes": "'odd'",
                "attribute-missing": "'warn'",
                "include-java": "'example$docs-src/main/java'",
            }
        )
        self.assertEqual(found, [])

    def test_ignores_keys_outside_the_attributes_block(self):
        """Regression: a loose 4-space match swept in `ext:` and made 5 look like 14."""
        found = self._run_with(
            {"used-site": "'https://a.example'"},
            docs="{used-site}\n",
            ext={"run": "'https://never.referenced.example'"},
        )
        self.assertEqual(found, [])

    def test_interpolated_value_counts_as_a_link(self):
        found = self._run_with(
            {"base": "'https://a.example'", "dead": "'{base}/gone'"},
            docs="only {base} is used\n",
        )
        self.assertEqual([c.identity for c in found], ["antora_attribute:dead"])

    def test_absent_antora_yml_is_fine(self):
        self.assertEqual(_run(_tree(sources=_LIVE_SOURCE)), [])

    def test_no_deletion_commit_field_for_attributes(self):
        """An attribute is a YAML key; a deletion commit is meaningless for it."""
        found = self._run_with(
            {"used": "'https://a.example'", "dead": "'https://b.example'"},
            docs="{used}\n",
        )
        self.assertNotIn("removed_in", found[0].evidence)
        self.assertEqual(found[0].evidence["value"], "'https://b.example'")

    def test_locus_points_at_the_declaration_line(self):
        found = self._run_with(
            {"used": "'https://a.example'", "dead": "'https://b.example'"},
            docs="{used}\n",
        )
        candidate = found[0]
        self.assertEqual(candidate.locus.path, _ANTORA)


class GeneralTests(unittest.TestCase):
    def test_absent_config_files_yield_nothing(self):
        self.assertEqual(_run(_tree(sources=_LIVE_SOURCE)), [])

    def test_locus_points_at_the_entry_line(self):
        root = _tree(suppressions=["Alive", "Gone_Thing"], sources=_LIVE_SOURCE)
        found = _run(root)[0]
        line = (root / _SUPPRESSIONS).read_text().splitlines()[found.locus.line - 1]
        self.assertIn("Gone_Thing", line)

    def test_is_deterministic(self):
        root = _tree(
            suppressions=["Gone_A", "Gone_B"],
            allowlist=["^http://gone.example.com.*", "^http://live.example.com.*"],
            sources={**_LIVE_SOURCE, "docs/x.adoc": "http://live.example.com/a\n"},
        )
        self.assertEqual(
            [c.to_jsonl() for c in _run(root)], [c.to_jsonl() for c in _run(root)]
        )

    def test_output_is_sorted_by_fingerprint(self):
        # Live entries included deliberately: an all-dead file trips the
        # wholly-dead guard, which is correct behaviour rather than a bug.
        sources = {
            f"mod/src/main/java/org/springframework/util/Alive{i}.java": ""
            for i in range(20)
        }
        root = _tree(
            suppressions=[f"Gone_{i}" for i in range(4)]
            + [f"Alive{i}" for i in range(20)],
            sources=sources,
        )
        found = _run(root)
        self.assertEqual(len(found), 4)
        self.assertEqual(
            [c.fingerprint for c in found], sorted(c.fingerprint for c in found)
        )


if __name__ == "__main__":
    unittest.main()
