"""Index and FQN-resolution behaviour, including two real regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from candidateminer.index import (
    TypeIndex,
    is_denylisted,
    is_type_reference,
    simple_name_of,
)


def _tree(files: dict[str, str]) -> Path:
    root = Path(tempfile.mkdtemp())
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(files[rel], encoding="utf-8")
    return root


_SAMPLE = {
    "spring-core/src/main/java/org/springframework/util/StringUtils.java": "",
    "spring-context/src/main/java/org/springframework/context/support/Thing.java": "",
    "spring-beans/src/testFixtures/java/org/springframework/beans/testfixture/Fixture.java": "",
    "spring-web/src/main/java/org/springframework/http/HttpMessageConverters.java": "",
    "spring-aspects/src/main/java/org/springframework/beans/factory/aspectj/Aspect.aj": "",
    "spring-core/src/main/kotlin/org/springframework/core/Kt.kt": "",
}


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.index = TypeIndex.build(_tree(_SAMPLE))

    def test_indexes_all_source_sets_and_languages(self):
        for fqn in (
            "org.springframework.util.StringUtils",
            "org.springframework.beans.testfixture.Fixture",
            "org.springframework.beans.factory.aspectj.Aspect",
            "org.springframework.core.Kt",
        ):
            self.assertIn(fqn, self.index.fqns, fqn)

    def test_separates_published_api_from_test_sources(self):
        self.assertTrue(self.index.is_public_api("org.springframework.util.StringUtils"))
        self.assertFalse(
            self.index.is_public_api("org.springframework.beans.testfixture.Fixture")
        )


class TargetPackageRegressionTests(unittest.TestCase):
    """`org.springframework.aop.target` is a package, not a build directory.

    Skipping directories named `target` by name erased five existing classes from
    the index and manufactured five confident false positives. Build-output names
    must only be skipped outside a source root.
    """

    def test_indexes_a_package_named_target(self):
        root = _tree(
            {
                "spring-aop/src/main/java/org/springframework/aop/target/"
                "HotSwappableTargetSource.java": "",
                # A genuine build directory, which must still be skipped.
                "spring-aop/build/classes/org/springframework/aop/Generated.java": "",
            }
        )
        index = TypeIndex.build(root)
        self.assertTrue(
            index.resolve("org.springframework.aop.target.HotSwappableTargetSource")
        )
        self.assertNotIn("org.springframework.aop.Generated", index.fqns)

    def test_skips_build_output_outside_source_roots(self):
        root = _tree({"build/tmp/org/springframework/Junk.java": ""})
        self.assertEqual(len(TypeIndex.build(root).fqns), 0)


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.index = TypeIndex.build(_tree(_SAMPLE))

    def test_resolves_exact(self):
        self.assertTrue(self.index.resolve("org.springframework.util.StringUtils"))

    def test_resolves_nested_type_to_its_enclosing_file(self):
        self.assertTrue(
            self.index.resolve("org.springframework.http.HttpMessageConverters.Builder")
        )

    def test_resolves_member_reference(self):
        self.assertTrue(
            self.index.resolve("org.springframework.util.StringUtils.hasText")
        )

    def test_does_not_resolve_absent_type(self):
        """The parent segment is a lowercase package, so stripping must stop."""
        self.assertFalse(self.index.resolve("org.springframework.oxm.jibx.JibxMarshaller"))

    def test_does_not_resolve_absent_type_in_known_package(self):
        self.assertFalse(self.index.resolve("org.springframework.util.NoSuchThing"))


class RelocationTests(unittest.TestCase):
    def test_unique_public_match_is_reported(self):
        index = TypeIndex.build(_tree(_SAMPLE))
        self.assertEqual(
            index.main_relocations_for("org.springframework.wrong.package.Thing"),
            ("org.springframework.context.support.Thing",),
        )

    def test_test_only_match_is_not_public(self):
        index = TypeIndex.build(_tree(_SAMPLE))
        self.assertEqual(
            index.relocations_for("org.springframework.elsewhere.Fixture"),
            ("org.springframework.beans.testfixture.Fixture",),
        )
        self.assertEqual(index.main_relocations_for("org.springframework.elsewhere.Fixture"), ())


class FqnHelperTests(unittest.TestCase):
    def test_rejects_lowercase_tail_as_non_type(self):
        """Kotlin extension functions and DSL helpers are not types."""
        self.assertFalse(
            is_type_reference("org.springframework.messaging.rsocket.retrieveAndAwait")
        )
        self.assertFalse(is_type_reference("org.springframework.beans.factory.getBean"))

    def test_accepts_type_and_constant(self):
        self.assertTrue(is_type_reference("org.springframework.util.StringUtils"))
        self.assertTrue(is_type_reference("org.springframework.http.MediaType.APPLICATION_JSON"))

    def test_denylists_other_spring_projects(self):
        for fqn in (
            "org.springframework.data.r2dbc.connection.R2dbcTransactionManager",
            "org.springframework.samples.MyServiceImpl",
            "org.springframework.boot.SpringApplication",
            "org.springframework.ws.samples.airline.schema.Flight",
        ):
            self.assertTrue(is_denylisted(fqn), fqn)

    def test_does_not_denylist_framework_itself(self):
        self.assertFalse(is_denylisted("org.springframework.beans.factory.BeanFactory"))

    def test_simple_name_is_the_outermost_type(self):
        self.assertEqual(
            simple_name_of("org.springframework.http.HttpMessageConverters.Builder"),
            "HttpMessageConverters",
        )


if __name__ == "__main__":
    unittest.main()
