"""Deterministic scoring.

Integer arithmetic only -- floats would vary in their last bits across platforms
and break the byte-identical-output guarantee. Every input is emitted alongside
the total so a human or a downstream agent can audit *why* something ranked where
it did, rather than trusting an opaque number.

Maximum is 100.
"""

from __future__ import annotations

# How much each extractor has earned the right to be believed, measured against
# this checkout during Phase 1 recon:
#   xml_class_attr  7/7   unresolved-after-denylist were real
#   api_url         1/1   real
#   prose         ~6-10 of 42 real  -- hence experimental, and scored accordingly
EXTRACTOR_PRIOR = {
    "xml_class_attr": 40,
    "api_url": 40,
    "prose": 15,
}

# A reference a reader will copy verbatim is worse than one they merely click.
# Wrong XML gives ClassNotFoundException at runtime; a wrong javadoc URL 404s.
COPY_PASTE_RISK = {
    "xml_class_attr": 10,
    "api_url": 5,
    "prose": 0,
}

_MAX_STALENESS = 15
_DAYS_PER_YEAR = 365


# How much to trust a proposed replacement target.
#
# "unambiguous" requires the simple name to be *globally unique* in the tree and
# that sole match to be published API. Both halves are load-bearing. Requiring
# only the second produced a real false positive during development: a doc
# reference to the long-deleted `org.springframework.beans.TestBean` matched the
# unrelated `@TestBean` bean-override annotation and was confidently reported as
# a one-token fix. Five distinct types in this tree are named `TestBean`; a
# common simple name carries no information about where anything moved.
RELOCATION_CERTAINTY = {
    "unambiguous": 25,  # exactly one type has this name, and it is public API
    "ambiguous": 10,    # several candidates: a human must choose
    "test_only": 5,     # exists, but never published; fix is editorial
    "none": 0,          # deleted outright; fix is removal, not repair
}


CONFIG_KIND_PRIOR = {
    "checkstyle_suppression": 40,
    "nohttp_allowlist": 40,
    # Precedent for this cleanup is in the harvested review corpus: a maintainer
    # audited antora.yml in PR #31619, proposed removing the unreferenced
    # `issues-old` attribute, and removed the obsolete sections himself.
    "antora_attribute": 40,
}


def config_dead_entry_inputs(
    *,
    config_kind: str,
    names_a_literal: bool,
    removal_age_days: int | None,
) -> dict[str, int]:
    """Score a build-configuration entry whose target no longer exists.

    A pattern that is a plain literal (`Target_ClassFinder`) names one specific
    thing, so its absence is unambiguous. A regex or path glob might legitimately
    match nothing in a clean tree, so it earns far less confidence.
    """
    if removal_age_days is None:
        staleness = 0
    else:
        staleness = min(max(removal_age_days // _DAYS_PER_YEAR, 0), _MAX_STALENESS)

    return {
        "config_kind_prior": CONFIG_KIND_PRIOR.get(config_kind, 0),
        "names_a_literal": 25 if names_a_literal else 5,
        "staleness_years": staleness,
        "single_line_fix": 10,
    }


def docs_dead_ref_inputs(
    *,
    extractor: str,
    relocation_confidence: str,
    removal_age_days: int | None,
    simple_name_exists_elsewhere: bool,
) -> dict[str, int]:
    relocation_certainty = RELOCATION_CERTAINTY[relocation_confidence]

    if removal_age_days is None:
        staleness = 0
    else:
        staleness = min(max(removal_age_days // _DAYS_PER_YEAR, 0), _MAX_STALENESS)

    return {
        "extractor_precision_prior": EXTRACTOR_PRIOR.get(extractor, 0),
        "relocation_certainty": relocation_certainty,
        "staleness_years": staleness,
        "copy_paste_risk": COPY_PASTE_RISK.get(extractor, 0),
        "corroboration": 10 if simple_name_exists_elsewhere else 0,
    }


def flaky_test_inputs(
    *,
    occurrences: int,
    distinct_tasks: int,
    max_failed_in_one_run: int,
    has_line_evidence: bool,
) -> dict[str, int]:
    """Score a test that failed repeatedly on otherwise-green main.

    Recurrence dominates: two failures could still be two bad nights, five is a
    pattern. Distinct task variants (test / java21Test / java24Test) matter
    because a test failing across JDK variants is flaky in itself, not broken
    on one JDK. Isolation rewards the 1-failure-in-5000 shape and penalises
    runs that took neighbours down with them.
    """
    return {
        "recurrence": min(occurrences, 3) * 15,
        "task_variant_spread": min(distinct_tasks, 3) * 10,
        "isolation": 15 if max_failed_in_one_run <= 5 else 5,
        "line_evidence": 10 if has_line_evidence else 0,
    }


def total(inputs: dict[str, int]) -> int:
    return sum(inputs.values())
