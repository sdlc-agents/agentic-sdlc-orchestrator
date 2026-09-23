"""Work out which files a change touches, from the requirement and the import graph."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .code_scanner import CodebaseIndex

STOPWORDS = frozenset(
    """
    a add added adds all also an and any app application are as at be been
    being but by can change changed changes could current do does each
    every existing for from had has have how if in into is it its just
    make makes making many may might more most much must need needs new
    not of on only or other same service should so some such support
    supports system than that the their them then there these they this
    those to under up use used using very was were what when where which
    while will with without would you your
    """.split()
)

# Words describing the work rather than its subject.
NOISE = frozenset(
    """
    build design ensure expose feature implement implementation provide
    record records report reports requirement requirements return returns
    """.split()
)

WORD = re.compile(r"[a-z][a-z0-9_]{2,}")


def salient_terms(*texts: str) -> Counter[str]:
    """Content words from the requirement, singularised crudely.

    Counted rather than collected: a word the requirement keeps returning to is
    more likely to name the thing being built than one mentioned once.
    """
    terms: Counter[str] = Counter()
    for text in texts:
        for word in WORD.findall(text.lower()):
            if word in STOPWORDS or word in NOISE:
                continue
            terms[word] += 1
            if word.endswith("s") and len(word) > 4:
                terms[word[:-1]] += 1
    return terms


def _identifiers(name: str) -> set[str]:
    """Split CamelCase and snake_case into comparable words."""
    parts = re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+", name)
    return {p.lower() for p in parts if len(p) > 2}


def _overlaps(words: set[str], terms: set[str]) -> bool:
    """Exact match, or one contained in the other.

    Containment catches run-together names: a requirement about "short codes"
    should reach `shortcode.py`, which splits into a single word. Terms shorter
    than four characters are matched exactly, or every identifier would hit.
    """
    if words & terms:
        return True
    return any(
        len(term) >= 4 and (term in word or word in term)
        for term in terms
        for word in words
    )


@dataclass
class Match:
    path: str
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    direct: bool = True

    def hit(self, points: int, reason: str) -> None:
        self.score += points
        if reason not in self.reasons:
            self.reasons.append(reason)


def _score_module(module, terms: Counter[str], source: str) -> Match:
    match = Match(path=module.path)

    keys = set(terms)
    stem = Path(module.path).stem
    if _overlaps(_identifiers(stem), keys):
        match.hit(4, f"module name matches the requirement (`{stem}`)")

    for symbol in module.symbols:
        if _overlaps(_identifiers(symbol), keys):
            match.hit(3, f"defines `{symbol}`")

    for route in module.routes:
        path_words = _identifiers(route.path.replace("/", " "))
        if _overlaps(path_words, keys) or _overlaps(_identifiers(route.handler), keys):
            match.hit(4, f"serves {route.method} {route.path}")

    for table in module.tables:
        if _overlaps(_identifiers(table), keys):
            match.hit(3, f"owns table `{table}`")

    # A term appearing in the body is weak evidence on its own.
    body = set(WORD.findall(source.lower()))
    incidental = body & keys
    if incidental and match.score == 0:
        match.hit(1, f"mentions {', '.join(sorted(incidental)[:3])}")

    return match


def missing_concepts(index: CodebaseIndex, terms: Counter[str], min_mentions: int = 2) -> list[str]:
    """Recurring requirement terms that name nothing in the codebase yet.

    A term the requirement leans on repeatedly, which no module, symbol or table
    already provides, is a candidate for something that has to be built.
    """
    known: set[str] = set()
    for module in index.modules:
        known |= _identifiers(Path(module.path).stem)
        for symbol in module.symbols:
            known |= _identifiers(symbol)
        known |= {t.lower() for t in module.tables}

    def is_known(term: str) -> bool:
        # Crude stemming produces both "analytics" and "analytic"; treat a term
        # as known if either form already exists in the codebase.
        return term in known or f"{term}s" in known or term.rstrip("s") in known

    candidates = [
        (count, term)
        for term, count in terms.items()
        if not is_known(term) and len(term) > 4 and count >= min_mentions
    ]

    seen: set[str] = set()
    out: list[str] = []
    for _, term in sorted(candidates, reverse=True):
        root = term.rstrip("s")
        if root in seen:
            continue
        seen.add(root)
        out.append(term)
    return out


def analyse(
    index: CodebaseIndex,
    terms: Counter[str],
    read: Callable[[str], str],
    threshold: int = 3,
) -> tuple[list[Match], list[Match]]:
    """Return (directly matched modules, modules reached from them).

    Direct matches come from the requirement's own vocabulary. Propagated ones
    come from the import graph: a module a changed file depends on is at risk
    even when the requirement never names it.
    """
    scored: dict[str, Match] = {}
    for module in index.modules:
        if not module.path.endswith(".py") or module.path.endswith("__init__.py"):
            continue
        if Path(module.path).name.startswith("test_") or "conftest" in module.path:
            continue
        match = _score_module(module, terms, read(module.path))
        if match.score >= threshold:
            scored[module.path] = match

    direct = sorted(scored.values(), key=lambda m: (-m.score, m.path))

    propagated: dict[str, Match] = {}
    by_path = {m.path: m for m in index.modules}
    for seed in direct:
        if by_path.get(seed.path) is None:
            continue
        for other in index.modules:
            if other.path in scored or other.path in propagated:
                continue
            if not other.path.endswith(".py") or other.path.endswith("__init__.py"):
                continue
            if Path(other.path).name.startswith("test_") or "conftest" in other.path:
                continue

            # Both directions matter. What a changed file depends on can break
            # under it; what depends on a changed file has to keep working.
            depends_on_it = seed.path in index.importers_of(other.path)
            calls_it = other.path in index.importers_of(seed.path)
            if not (depends_on_it or calls_it):
                continue

            reason = (
                f"`{seed.path}` imports it, and that file changes"
                if depends_on_it
                else f"it imports `{seed.path}`, which changes"
            )
            reached = Match(path=other.path, score=1, direct=False)
            reached.reasons.append(reason)
            propagated[other.path] = reached

    return direct, sorted(propagated.values(), key=lambda m: m.path)
