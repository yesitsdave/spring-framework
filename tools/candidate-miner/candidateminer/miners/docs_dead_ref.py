"""Dead Spring API references in the reference documentation.

Why this category and not the obvious ones: the reference manual is the largest
body of text in the repo that **no tool validates**. `framework-docs` is excluded
from the build's convention plugins (`build.gradle:14` filters out `framework-*`),
and while `antora` does run on pull requests it only *renders* AsciiDoc -- it
never checks that a Java FQN in a snippet still exists. Meanwhile 216
`include-code::` directives bind to compiled sources and so cannot drift, but
~1169 inline `[source,java]` blocks compile against nothing at all. That split is
the fault line, and this miner works it.

The contrast measured during recon makes the point: FQN-qualified javadoc
references in enforced `src/main/java` resolve at 99.7%, because the aggregate
javadoc task runs with `Werror = true`. The same check against `.adoc` finds
references to types deleted in 2016 and 2021 still sitting in shipped docs.
Rot accumulates exactly where no tool looks.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from .. import gitutil, scoring
from ..contract import Candidate, CostClass, Locus, Score
from ..index import is_denylisted, is_type_reference, simple_name_of
from .base import MinerContext, MinerDefect

CATEGORY = "docs.dead_api_reference"

# An FQN sitting in a Spring bean definition. A reader copies this verbatim into
# their own XML, so a wrong package is a runtime ClassNotFoundException.
_RE_XML_CLASS = re.compile(
    r"""class\s*=\s*["'](org\.springframework\.[A-Za-z0-9_.$]+)["']"""
)

# A cross-link into the published javadoc. The attribute expands to a URL whose
# path already contains org/springframework (framework-docs/antora.yml:40), so
# the captured path must be prefixed, not treated as a whole FQN. Getting this
# backwards is precisely the bug that produced 227 false positives during recon.
_RE_API_URL = re.compile(r"\{spring-framework-api\}/([A-Za-z0-9_$/.]+?)\.html")

# Bare FQNs in prose and snippet imports. Much noisier: illustrative names and
# other Spring projects dominate. Experimental, off by default.
_RE_PROSE = re.compile(r"\borg\.springframework\.[A-Za-z0-9_.]*[A-Z][A-Za-z0-9_]*")

_EXTRACTOR_PRECEDENCE = ("xml_class_attr", "api_url", "prose")

_SKIP_DIRS = frozenset({".git", "build", "out", "bin", "node_modules", ".gradle"})

_MAX_LINE_LEN = 200

# Sanity ceiling for the self-check. Recon measured the worst real rate at ~17%
# (xml_class_attr, before denylisting). Anything above 25% on a decent sample
# means the extractor is broken, not that the docs are.
_DEFAULT_SANITY_CEILING = 0.25
_SANITY_MIN_SAMPLE = 20

# The mirror-image blind spot: every ceiling above guards against too *many*
# findings, but a broken extractor that matches nothing at all would sail
# through with an empty, plausible-looking result. A docs tree of this many
# pages with zero extractable references means the regexes broke, not that
# the docs stopped citing the framework.
_ZERO_SAMPLE_MIN_FILES = 50


@dataclass(frozen=True)
class _Ref:
    fqn: str
    path: str
    line: int
    text: str
    extractor: str


class DocsDeadRefMiner:
    name = "docs_dead_ref"
    cost_class = CostClass.B_SOURCE
    version = "1.0.0"
    description = "Spring API references in .adoc docs that no longer exist in the tree"

    def run(self, ctx: MinerContext) -> Iterable[Candidate]:
        include_experimental = bool(ctx.options.get("include_experimental"))
        ceiling = float(ctx.options.get("sanity_ceiling", _DEFAULT_SANITY_CEILING))
        index = ctx.type_index

        adoc_files = list(_find_adoc_files(ctx.root))
        refs = [
            ref
            for adoc in adoc_files
            for ref in _extract_from(adoc, ctx.root, include_experimental)
            if is_type_reference(ref.fqn) and not is_denylisted(ref.fqn)
        ]
        if len(adoc_files) >= _ZERO_SAMPLE_MIN_FILES and not refs:
            raise MinerDefect(
                f"scanned {len(adoc_files)} .adoc files and extracted zero Spring "
                f"API references. A docs tree of this size always cites the "
                f"framework; the extractors are almost certainly broken. "
                f"Refusing to emit an empty result as if it were clean."
            )

        unresolved = [r for r in refs if not index.resolve(r.fqn)]
        _self_check(refs, unresolved, ceiling)

        reference_date = _reference_date(ctx.root)

        # One dead reference per file is one candidate, however many times it
        # appears -- it is a single fix, and this keeps the fingerprint immune to
        # line churn and to occurrences being added or removed.
        grouped: dict[tuple[str, str], list[_Ref]] = defaultdict(list)
        for ref in unresolved:
            grouped[(ref.path, ref.fqn)].append(ref)

        candidates = [
            self._build(ctx, index, path, fqn, occurrences, reference_date)
            for (path, fqn), occurrences in grouped.items()
        ]
        return sorted(candidates, key=lambda c: c.fingerprint)

    def _build(
        self,
        ctx: MinerContext,
        index,
        path: str,
        fqn: str,
        occurrences: list[_Ref],
        reference_date: date | None,
    ) -> Candidate:
        occurrences = sorted(occurrences, key=lambda r: (r.line, r.extractor))
        extractors = sorted({r.extractor for r in occurrences})
        primary = min(extractors, key=_EXTRACTOR_PRECEDENCE.index)

        relocations = index.relocations_for(fqn)
        main_relocations = index.main_relocations_for(fqn)
        removal = gitutil.last_deletion_of(ctx.root, simple_name_of(fqn))
        removal_age = _age_days(removal[1], reference_date) if removal else None

        confidence = _relocation_confidence(relocations, main_relocations)

        inputs = scoring.docs_dead_ref_inputs(
            extractor=primary,
            relocation_confidence=confidence,
            removal_age_days=removal_age,
            simple_name_exists_elsewhere=bool(main_relocations),
        )

        evidence = {
            "reference": fqn,
            "primary_extractor": primary,
            "extractors": extractors,
            "resolution": "unresolved",
            "occurrences": [
                {"line": r.line, "text": r.text} for r in _dedupe_by_line(occurrences)
            ],
            "relocated_to": list(relocations),
            "relocated_to_public_api": list(main_relocations),
            "relocation_confidence": confidence,
            "removed_in": (
                {"commit": removal[0], "date": removal[1]} if removal else None
            ),
            "simple_name_exists_elsewhere": bool(main_relocations),
            # True when every same-named type left in the tree lives in a test or
            # testFixtures source set: the docs are illustrating with something
            # that was never published API.
            "illustrative_only": confidence == "test_only",
        }

        return Candidate(
            category=CATEGORY,
            cost_class=self.cost_class,
            miner_name=self.name,
            miner_version=self.version,
            locus=Locus(path=path, line=occurrences[0].line),
            identity=fqn,
            evidence=evidence,
            score=Score(value=scoring.total(inputs), inputs=inputs),
            pr_grouping_key=path,
            repo_commit=ctx.commit,
            repo_branch=ctx.branch,
        )


def _relocation_confidence(
    relocations: tuple[str, ...], main_relocations: tuple[str, ...]
) -> str:
    """How much to trust a proposed replacement.

    A replacement is only unambiguous when the simple name is unique across the
    *entire* tree and that sole match is published API. Uniqueness alone is not
    enough (it might be a test fixture), and being public API alone is not enough
    either -- common names like ``TestBean`` appear five times here, and matching
    on one of them invents a relocation that never happened.
    """
    if len(relocations) == 1 and len(main_relocations) == 1:
        return "unambiguous"
    if main_relocations:
        return "ambiguous"
    if relocations:
        return "test_only"
    return "none"


def _dedupe_by_line(refs: list[_Ref]) -> list[_Ref]:
    seen: set[int] = set()
    out = []
    for ref in refs:
        if ref.line not in seen:
            seen.add(ref.line)
            out.append(ref)
    return out


def _self_check(refs: list[_Ref], unresolved: list[_Ref], ceiling: float) -> None:
    """Refuse to emit when an extractor's miss rate is implausible.

    Measured per extractor on *distinct* references, which is how the recon
    numbers were established and the only way a single spammy file cannot skew
    the result.
    """
    per_extractor: dict[str, set[str]] = defaultdict(set)
    per_extractor_bad: dict[str, set[str]] = defaultdict(set)
    for ref in refs:
        per_extractor[ref.extractor].add(ref.fqn)
    for ref in unresolved:
        per_extractor_bad[ref.extractor].add(ref.fqn)

    for extractor, all_fqns in sorted(per_extractor.items()):
        total = len(all_fqns)
        if total < _SANITY_MIN_SAMPLE:
            continue
        bad = len(per_extractor_bad.get(extractor, ()))
        rate = bad / total
        if rate > ceiling:
            raise MinerDefect(
                f"extractor {extractor!r} reports {bad}/{total} references "
                f"unresolved ({rate:.0%}), above the {ceiling:.0%} sanity ceiling. "
                f"This almost certainly means the extractor is broken -- most "
                f"likely FQN normalisation -- not that the docs are. Refusing to "
                f"emit. Re-run with --sanity-ceiling to override if the rate is "
                f"genuinely expected."
            )


def _reference_date(root: Path) -> date | None:
    """HEAD's commit date, used as 'now'.

    Wall-clock time would make scores change between runs on the same commit and
    break the byte-identical-output guarantee.
    """
    return _parse_date(gitutil.head_date(root))


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _age_days(removal_date: str, reference: date | None) -> int | None:
    parsed = _parse_date(removal_date)
    if parsed is None or reference is None:
        return None
    return max((reference - parsed).days, 0)


def _find_adoc_files(root: Path) -> Iterator[Path]:
    stack = [root]
    found: list[Path] = []
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix == ".adoc":
                found.append(entry)
    yield from sorted(found)


def _extract_from(path: Path, root: Path, include_experimental: bool) -> Iterator[_Ref]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    rel = path.relative_to(root).as_posix()

    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()[:_MAX_LINE_LEN]

        for match in _RE_XML_CLASS.finditer(line):
            yield _Ref(match.group(1), rel, lineno, stripped, "xml_class_attr")

        for match in _RE_API_URL.finditer(line):
            fqn = "org.springframework." + match.group(1).replace("/", ".")
            yield _Ref(fqn, rel, lineno, stripped, "api_url")

        if include_experimental:
            for match in _RE_PROSE.finditer(line):
                yield _Ref(match.group(0).rstrip("."), rel, lineno, stripped, "prose")


MINER = DocsDeadRefMiner()
