"""Build-configuration entries whose target no longer exists.

Same question as `docs_dead_ref`, different corpus: *this references that -- does
that still exist?* Here the references live in checkstyle suppression patterns and
the nohttp allowlist, and the referents are files in the tree.

Why it is worth mining: `src/checkstyle/checkstyle-suppressions.xml` is a curated
inventory of deliberate exceptions, which is exactly why nobody re-reads it. An
entry keeps working after its target is deleted -- it simply matches nothing and
stays silent forever. Discovery cost is high (96 patterns, each needing a
whole-tree match), fix cost is one deleted line.

Two confirmed at the time of writing, both suppressing checks for GraalVM/Hibernate
substitution classes removed in `59a9d0fcb9` and `2350c8a8f5`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterable, Iterator

from .. import gitutil, scoring
from ..contract import Candidate, CostClass, Locus, Score
from ..index import list_repo_files
from .base import MinerContext, MinerDefect

CATEGORY = "config.dead_entry"

CHECKSTYLE_SUPPRESSIONS = "src/checkstyle/checkstyle-suppressions.xml"
NOHTTP_ALLOWLIST = "src/nohttp/allowlist.lines"
ANTORA_YML = "framework-docs/antora.yml"

_RE_SUPPRESS_FILES = re.compile(r'files="([^"]+)"')

# Matches how nohttp finds URLs to check, so allowlist patterns are tested
# against the same strings they were written for.
_RE_URL = re.compile(r"""https?://[^\s"'<>)\]}\\,;]+""")

# Path segments that only exist after a build. A suppression aimed at generated
# or compiled output legitimately matches nothing in a clean checkout, so treating
# it as dead would be a false positive -- this is the one such entry in the file
# (`[\\/]build[\\/]generated[\\/]sources[\\/]`) and it must not be reported.
_BUILD_OUTPUT_SEGMENTS = ("build", "generated", "out", "bin", "classes")

# Anything beyond these makes a pattern a regex rather than a literal name.
_REGEX_METACHARACTERS = set(r"\[](){}|?*+^$.")

_TEXT_SUFFIXES = frozenset(
    {
        ".java", ".kt", ".aj", ".xml", ".adoc", ".gradle", ".properties", ".txt",
        ".md", ".html", ".js", ".css", ".sql", ".yml", ".yaml", ".xsd", ".factories",
        ".lines", ".kts", ".groovy",
    }
)
_MAX_SCAN_BYTES = 2_000_000

_DEFAULT_SANITY_CEILING = 0.25
_SANITY_MIN_SAMPLE = 20
_DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class _Entry:
    kind: str
    pattern: str
    path: str
    line: int
    raw_line: str
    value: str = ""


class ConfigDeadEntryMiner:
    name = "config_dead_entry"
    cost_class = CostClass.B_SOURCE
    version = "1.0.0"
    description = "checkstyle/nohttp/antora config entries whose target no longer exists"

    def run(self, ctx: MinerContext) -> Iterable[Candidate]:
        ceiling = float(ctx.options.get("sanity_ceiling", _DEFAULT_SANITY_CEILING))
        repo_files = list_repo_files(ctx.root)
        reference_date = _parse_date(gitutil.head_date(ctx.root))

        suppressions = list(_read_suppressions(ctx.root))
        allowlist = list(_read_allowlist(ctx.root))
        all_attributes = list(_read_antora_attributes(ctx.root))
        attributes = [e for e in all_attributes if _is_link_valued(e.value)]

        # Zero-extraction guard, the mirror image of the ceilings below: a file
        # that visibly contains entries but parses to none means the parser
        # broke, and an empty result would masquerade as a clean bill of health.
        _check_extraction(ctx.root, CHECKSTYLE_SUPPRESSIONS, "<suppress", suppressions)
        _check_extraction(ctx.root, ANTORA_YML, "attributes:", all_attributes)

        dead_suppressions = [e for e in suppressions if _suppression_is_dead(e, repo_files)]
        # The allowlist must not be scanned for its own URLs: every pattern
        # contains the URL it permits, so including the file makes each entry
        # match itself and nothing can ever be reported dead.
        matched_urls = _scan_for_patterns(
            ctx.root,
            [e.pattern for e in allowlist],
            repo_files,
            exclude={NOHTTP_ALLOWLIST},
        )
        dead_allowlist = [e for e in allowlist if e.pattern not in matched_urls]

        dead_attributes = _dead_antora_attributes(attributes, ctx.root, repo_files)

        _self_check("checkstyle_suppression", suppressions, dead_suppressions, ceiling)
        _self_check("nohttp_allowlist", allowlist, dead_allowlist, ceiling)
        _self_check("antora_attribute", attributes, dead_attributes, ceiling)

        candidates = [
            self._build(ctx, entry, reference_date)
            for entry in dead_suppressions + dead_allowlist + dead_attributes
        ]
        return sorted(candidates, key=lambda c: c.fingerprint)

    def _build(
        self, ctx: MinerContext, entry: _Entry, reference_date: date | None
    ) -> Candidate:
        literal = _is_literal(entry.pattern)
        removal = (
            gitutil.last_deletion_of(ctx.root, entry.pattern)
            if literal and entry.pattern.isidentifier()
            else None
        )
        removal_age = (
            _age_days(removal[1], reference_date) if removal and reference_date else None
        )

        inputs = scoring.config_dead_entry_inputs(
            config_kind=entry.kind,
            names_a_literal=literal,
            removal_age_days=removal_age,
        )

        evidence = {
            "pattern": entry.pattern,
            "config_kind": entry.kind,
            "names_a_literal": literal,
            "matches_nothing_in_tree": True,
            "raw_line": entry.raw_line,
            "value": entry.value,
        }
        # A deletion commit only means something for entries that name a file.
        # An attribute is a key in a YAML map; reporting "no deletion commit" for
        # one is noise dressed as evidence.
        if entry.kind != "antora_attribute":
            evidence["removed_in"] = (
                {"commit": removal[0], "date": removal[1]} if removal else None
            )

        return Candidate(
            category=CATEGORY,
            cost_class=self.cost_class,
            miner_name=self.name,
            miner_version=self.version,
            locus=Locus(path=entry.path, line=entry.line),
            identity=f"{entry.kind}:{entry.pattern}",
            evidence=evidence,
            score=Score(value=scoring.total(inputs), inputs=inputs),
            pr_grouping_key=entry.path,
            repo_commit=ctx.commit,
            repo_branch=ctx.branch,
        )


def _read_suppressions(root: Path) -> Iterator[_Entry]:
    path = root / CHECKSTYLE_SUPPRESSIONS
    if not path.is_file():
        return
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        for match in _RE_SUPPRESS_FILES.finditer(line):
            yield _Entry(
                kind="checkstyle_suppression",
                pattern=match.group(1),
                path=CHECKSTYLE_SUPPRESSIONS,
                line=lineno,
                raw_line=line.strip()[:200],
            )


def _read_allowlist(root: Path) -> Iterator[_Entry]:
    path = root / NOHTTP_ALLOWLIST
    if not path.is_file():
        return
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield _Entry(
            kind="nohttp_allowlist",
            pattern=stripped,
            path=NOHTTP_ALLOWLIST,
            line=lineno,
            raw_line=stripped[:200],
        )


def _read_antora_attributes(root: Path) -> Iterator[_Entry]:
    """Attributes declared under `asciidoc: attributes:` in antora.yml.

    Indentation-parsed rather than YAML-parsed: the standard library has no YAML
    reader, and scoping matters. A first attempt matched 4-space-indented keys
    anywhere in the file and swept in the unrelated `ext:` block (`run`, `scan`),
    which is how 5 real findings first presented as 14.
    """
    path = root / ANTORA_YML
    if not path.is_file():
        return
    in_asciidoc = False
    in_attributes = False
    for lineno, line in enumerate(
        path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            in_asciidoc = line.strip() == "asciidoc:"
            in_attributes = False
            continue
        if in_asciidoc and indent == 2:
            in_attributes = line.strip() == "attributes:"
            continue
        if in_attributes:
            if indent < 4:
                in_attributes = False
                continue
            match = re.match(r"^\s{4}([A-Za-z0-9][A-Za-z0-9_-]*):\s*(.*)$", line)
            if match:
                yield _Entry(
                    kind="antora_attribute",
                    pattern=match.group(1),
                    path=ANTORA_YML,
                    line=lineno,
                    raw_line=line.strip()[:200],
                    value=match.group(2).strip(),
                )


def _is_link_valued(value: str) -> bool:
    """Only consider attributes that hold a link.

    This replaces a hand-maintained denylist of Asciidoctor built-ins, and is
    self-justifying: `chomp: 'all'`, `table-stripes: 'odd'` and
    `include-java: 'example$docs-src/...'` are settings and resource paths, not
    links, so they are out of scope without having to be named. A leading `{`
    counts, since several link attributes are built by interpolating others.
    """
    stripped = value.strip().strip("'\"")
    return stripped.startswith(("http://", "https://", "{"))


def _dead_antora_attributes(
    entries: list[_Entry], root: Path, repo_files: tuple[str, ...]
) -> list[_Entry]:
    """Link attributes never referenced as `{name}` anywhere in the docs.

    antora.yml is included in the corpus because attributes legitimately
    interpolate one another.

    Known limitation, deliberately not handled: this does not cascade. If a dead
    attribute is the sole referrer of another, that second one stays hidden until
    the first is removed and the miner runs again. Chasing the transitive closure
    would risk proposing removals whose justification depends on an unmerged change.
    """
    if not entries:
        return []
    corpus_parts = [(root / ANTORA_YML).read_text(encoding="utf-8", errors="replace")]
    for rel in repo_files:
        if rel.endswith(".adoc"):
            try:
                corpus_parts.append(
                    (root / rel).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
    corpus = "\n".join(corpus_parts)
    referenced = set(re.findall(r"\{([A-Za-z0-9][A-Za-z0-9_-]*)\}", corpus))
    return [e for e in entries if e.pattern not in referenced]


def _suppression_is_dead(entry: _Entry, repo_files: tuple[str, ...]) -> bool:
    if _targets_build_output(entry.pattern):
        return False
    compiled = _compile(entry.pattern)
    if compiled is None:
        # An unparseable pattern is a different defect; do not claim it is dead.
        return False
    return not any(compiled.search(f) for f in repo_files)


def _targets_build_output(pattern: str) -> bool:
    lowered = pattern.lower()
    return any(seg in lowered for seg in _BUILD_OUTPUT_SEGMENTS)


def _compile(pattern: str) -> re.Pattern[str] | None:
    """Checkstyle patterns are Java regexes.

    The constructs used here -- character classes such as ``[\\\\/]``, alternation,
    and negative lookahead -- carry the same meaning in Python, so the pattern is
    used verbatim rather than translated. Translation would be a source of exactly
    the normalisation bug this tool is built to avoid.
    """
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _scan_for_patterns(
    root: Path,
    patterns: list[str],
    repo_files: tuple[str, ...],
    *,
    exclude: set[str] = frozenset(),
) -> set[str]:
    """Return the allowlist patterns that still permit a URL present in the tree.

    Semantics must match nohttp's: it extracts URLs and tests each *URL* against
    the allowlist, so the patterns are anchored with ``^``. Applying them to raw
    file text instead makes every anchored pattern fail, because the URL sits
    mid-line -- which reported all six entries as dead on the first attempt.
    """
    outstanding = {p: c for p in patterns if (c := _compile(p)) is not None}
    matched: set[str] = {p for p in patterns if p not in outstanding}
    for rel in repo_files:
        if not outstanding:
            break
        if rel in exclude:
            continue
        path = root / rel
        if path.suffix not in _TEXT_SUFFIXES:
            continue
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        urls = set(_RE_URL.findall(text))
        if not urls:
            continue
        for pattern in [
            p for p, c in outstanding.items() if any(c.search(u) for u in urls)
        ]:
            matched.add(pattern)
            del outstanding[pattern]
    return matched


def _is_literal(pattern: str) -> bool:
    return not (set(pattern) & _REGEX_METACHARACTERS)


def _check_extraction(root: Path, rel: str, marker: str, entries: list[_Entry]) -> None:
    """Defect when a config file contains its marker but parsed to nothing.

    Keyed on the marker so a legitimately minimal file (an antora.yml with no
    attributes block, say) stays silent, while a format change that breaks the
    parser fails loudly instead of emitting an empty, clean-looking result.
    """
    if entries:
        return
    path = root / rel
    if not path.is_file():
        return
    if marker in path.read_text(encoding="utf-8", errors="replace"):
        raise MinerDefect(
            f"{rel} contains {marker!r} but zero entries were parsed from it. "
            f"The file format has likely changed out from under the parser. "
            f"Refusing to emit an empty result as if the config were clean."
        )


def _self_check(
    kind: str, all_entries: list[_Entry], dead: list[_Entry], ceiling: float
) -> None:
    if not all_entries:
        return
    rate = len(dead) / len(all_entries)

    # A small file can be 100% "dead" and still sit under the sample minimum --
    # which is exactly what happened when the nohttp matcher used the wrong
    # semantics and condemned all six entries. Every entry being dead is
    # implausible at any size, so check that independently of sample size.
    if rate == 1.0 and len(all_entries) >= 3:
        raise MinerDefect(
            f"{kind}: every one of {len(all_entries)} entries matches nothing. "
            f"A curated config file is never wholly dead -- the matcher semantics "
            f"are almost certainly wrong. Refusing to emit."
        )

    if len(all_entries) < _SANITY_MIN_SAMPLE:
        return
    if rate > ceiling:
        raise MinerDefect(
            f"{kind}: {len(dead)}/{len(all_entries)} entries "
            f"({rate:.0%}) match nothing, above the {ceiling:.0%} sanity ceiling. "
            f"A curated config file is not mostly dead -- the matcher is far more "
            f"likely to be broken. Refusing to emit."
        )


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _age_days(removal_date: str, reference: date) -> int | None:
    parsed = _parse_date(removal_date)
    return None if parsed is None else max((reference - parsed).days, 0)


MINER = ConfigDeadEntryMiner()
