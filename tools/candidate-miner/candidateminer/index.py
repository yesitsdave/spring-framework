"""An index of every type FQN that actually exists in the checkout.

Resolution deliberately errs toward *resolving*. A false "this exists" costs a
missed candidate; a false "this is missing" costs a bogus one. In a
precision-first tool the second is far more expensive, so every ambiguity is
resolved in favour of the reference being fine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Any Maven/Gradle-style source root: src/<sourceSet>/<lang>/...
# Covers main, test, testFixtures, jmh, and the java21/java24 multi-release sets.
_SOURCE_ROOT_RE = re.compile(r"(?:^|/)src/([^/]+)/(?:java|java\d+|kotlin)/")

# Only `main` is published API. A type that exists solely in test or testFixtures
# is not something documentation should be pointing readers at, and a relocation
# target in those trees is not a usable replacement.
_MAIN_SOURCE_SET = "main"

_INDEXED_SUFFIXES = (".java", ".aj", ".kt")

# Build-output and tooling directories, skipped only *outside* a source root.
# Several of these names are also legitimate Java packages -- `org.springframework
# .aop.target` is a real one, and skipping it by name erased five existing classes
# from the index and manufactured five false positives. Once we are inside a
# `src/<set>/<lang>/` root, every directory is a package and nothing is skipped.
_SKIP_DIRS = frozenset(
    {".git", "build", "out", "bin", "node_modules", ".gradle", ".idea", "target"}
)

# Other Spring *projects*. A reference to these is not a Spring Framework type
# and its absence from this tree proves nothing. Without this denylist the
# `org.springframework.samples.*` illustrative names alone would halve precision.
DENYLIST_PREFIXES: tuple[str, ...] = (
    "org.springframework.samples.",
    "org.springframework.ws.",
    "org.springframework.data.",
    "org.springframework.boot.",
    "org.springframework.security.",
    "org.springframework.batch.",
    "org.springframework.integration.",
    "org.springframework.cloud.",
    "org.springframework.session.",
    "org.springframework.graphql.",
    "org.springframework.hateoas.",
    "org.springframework.amqp.",
    "org.springframework.kafka.",
    "org.springframework.shell.",
    "org.springframework.restdocs.",
    "org.springframework.modulith.",
)


def is_denylisted(fqn: str) -> bool:
    return fqn.startswith(DENYLIST_PREFIXES)


def is_type_reference(fqn: str) -> bool:
    """True if ``fqn`` plausibly names a type (or a member of one).

    Filters out Kotlin extension-function FQNs such as
    ``org.springframework.messaging.rsocket.retrieveAndAwait`` and DSL helpers
    like ``org.springframework.beans.factory.getBean``, whose final segment is
    lowercase. Those are functions, not types; the index cannot speak to them.
    """
    if "." not in fqn:
        return False
    last = fqn.rsplit(".", 1)[1]
    return bool(last) and last[0].isupper()


def simple_name_of(fqn: str) -> str:
    """Outermost type name -- the part that maps to a file on disk."""
    for part in fqn.split("."):
        if part[:1].isupper():
            return part
    return fqn.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class TypeIndex:
    fqns: frozenset[str]
    main_fqns: frozenset[str]
    by_simple_name: dict[str, tuple[str, ...]]

    @classmethod
    def build(cls, root: Path) -> "TypeIndex":
        found: set[str] = set()
        main: set[str] = set()
        for path in _walk(root):
            posix = path.as_posix()
            match = _SOURCE_ROOT_RE.search(posix)
            if not match:
                continue
            rel = posix[match.end():]
            for suffix in _INDEXED_SUFFIXES:
                if rel.endswith(suffix):
                    rel = rel[: -len(suffix)]
                    break
            fqn = rel.replace("/", ".")
            found.add(fqn)
            if match.group(1) == _MAIN_SOURCE_SET:
                main.add(fqn)

        by_simple: dict[str, list[str]] = {}
        for fqn in found:
            by_simple.setdefault(fqn.rsplit(".", 1)[-1], []).append(fqn)
        return cls(
            fqns=frozenset(found),
            main_fqns=frozenset(main),
            by_simple_name={k: tuple(sorted(v)) for k, v in by_simple.items()},
        )

    def is_public_api(self, fqn: str) -> bool:
        return fqn in self.main_fqns

    def __len__(self) -> int:
        return len(self.fqns)

    def resolve(self, fqn: str) -> bool:
        """True if ``fqn`` names something that exists, allowing for nesting.

        Walks outward from the reference, stripping trailing capitalised
        segments. This is what makes ``HttpMessageConverters.Builder`` (nested
        type) and ``MediaType.APPLICATION_JSON`` (constant) resolve to their
        enclosing file, while ``oxm.jibx.JibxMarshaller`` -- whose parent segment
        is the lowercase package ``jibx`` -- correctly does not.
        """
        current = fqn
        while True:
            if current in self.fqns:
                return True
            if "." not in current:
                return False
            head, _ = current.rsplit(".", 1)
            # Only keep stripping while the enclosing segment is itself a type.
            # Once the parent is a package, the reference was top-level and absent.
            if not head.rsplit(".", 1)[-1][:1].isupper():
                return False
            current = head

    def relocations_for(self, fqn: str) -> tuple[str, ...]:
        """Existing types sharing this simple name at a different FQN.

        A single hit is strong evidence the type *moved* rather than being
        deleted, which turns the candidate into a one-token correction.
        """
        simple = simple_name_of(fqn)
        return tuple(c for c in self.by_simple_name.get(simple, ()) if c != fqn)

    def main_relocations_for(self, fqn: str) -> tuple[str, ...]:
        """Relocation targets that are actually published API.

        This is what separates "the class moved to `context.support`, fix the
        package" from "this names a test fixture that documentation should never
        have pointed at". Both are real dead references, but only the first has
        a mechanical one-token fix, and the score must not confuse them.
        """
        return tuple(c for c in self.relocations_for(fqn) if c in self.main_fqns)


def list_repo_files(root: Path) -> tuple[str, ...]:
    """Every file in the tree as a repo-relative posix path, sorted.

    Build output is excluded, but only outside source roots -- same rule as the
    type index, and for the same reason (`target` is a package name here).
    """
    found: list[str] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        in_source_root = bool(_SOURCE_ROOT_RE.search(current.as_posix()))
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if in_source_root or entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            else:
                found.append(entry.relative_to(root).as_posix())
    return tuple(sorted(found))


def _walk(root: Path):
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        # Inside a source root every directory is a package name, so the
        # build-output denylist must no longer apply.
        in_source_root = bool(_SOURCE_ROOT_RE.search(current.as_posix()))
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if in_source_root or entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.suffix in _INDEXED_SUFFIXES:
                yield entry
