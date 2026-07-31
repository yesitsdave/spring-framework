"""Golden run against the actual Spring Framework checkout.

Asserts *properties* rather than a pinned list of findings. Pinning an exact set
would fail the moment a maintainer fixes one of these docs -- which is the
outcome the tool exists to cause. These assertions instead encode what must
always hold: nothing denylisted, nothing that actually resolves, every record
valid, and the highest-confidence known-dead references still detected.

Skips cleanly when run outside the checkout.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from candidateminer.contract import validate_record
from candidateminer.index import is_denylisted
from candidateminer.miners import MinerContext
from candidateminer.miners.docs_dead_ref import MINER

_REPO = Path(__file__).resolve().parents[3]

# Confirmed by hand during recon, each traced to a deletion commit. These are
# load-bearing regressions: if a change to the extractors stops finding them,
# the tool has lost the capability it was built for.
_KNOWN_DEAD = {
    "org.springframework.beans.factory.config.PropertySourcesPlaceholderConfigurer",
    "org.springframework.oxm.jibx.JibxMarshaller",
    "org.springframework.ejb.access.LocalStatelessSessionProxyFactoryBean",
    "org.springframework.jca.context.SpringContextResourceAdapter",
}


def _looks_like_spring_framework(root: Path) -> bool:
    return (root / "settings.gradle").is_file() and (root / "framework-docs").is_dir()


@unittest.skipUnless(
    _looks_like_spring_framework(_REPO), "not running inside a Spring Framework checkout"
)
class GoldenRepoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ctx = MinerContext(root=_REPO)
        cls.found = list(MINER.run(cls.ctx))

    def test_index_is_populated(self):
        self.assertGreater(len(self.ctx.type_index), 5000)
        self.assertGreater(len(self.ctx.type_index.main_fqns), 3000)

    def test_finds_the_known_dead_references(self):
        identities = {c.identity for c in self.found}
        missing = _KNOWN_DEAD - identities
        self.assertEqual(missing, set(), f"stopped detecting: {sorted(missing)}")

    def test_emits_nothing_denylisted(self):
        offenders = [c.identity for c in self.found if is_denylisted(c.identity)]
        self.assertEqual(offenders, [])

    def test_every_record_satisfies_the_contract(self):
        for candidate in self.found:
            validate_record(candidate.to_record())

    def test_no_emitted_reference_actually_resolves(self):
        """Self-consistency: a candidate that resolves is by definition a bug."""
        index = self.ctx.type_index
        resolvable = [c.identity for c in self.found if index.resolve(c.identity)]
        self.assertEqual(resolvable, [])

    def test_every_locus_points_at_a_real_file(self):
        for candidate in self.found:
            self.assertTrue(
                (_REPO / candidate.locus.path).is_file(), candidate.locus.path
            )

    def test_every_locus_line_contains_its_reference(self):
        """The advisory line must still be right at the commit it was mined from."""
        for candidate in self.found:
            lines = (_REPO / candidate.locus.path).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            line = lines[candidate.locus.line - 1]
            simple = candidate.identity.rsplit(".", 1)[-1]
            self.assertIn(simple, line, f"{candidate.locus.path}:{candidate.locus.line}")

    def test_fingerprints_are_unique(self):
        fingerprints = [c.fingerprint for c in self.found]
        self.assertEqual(len(fingerprints), len(set(fingerprints)))

    def test_results_are_deterministic(self):
        again = list(MINER.run(MinerContext(root=_REPO)))
        self.assertEqual(
            [c.to_jsonl() for c in self.found], [c.to_jsonl() for c in again]
        )

    def test_default_run_uses_only_high_precision_extractors(self):
        used = {c.evidence["primary_extractor"] for c in self.found}
        self.assertNotIn("prose", used)


if __name__ == "__main__":
    unittest.main()
