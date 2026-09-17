"""Local query normalization, explicit aliases and bounded spelling matches."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[-‐‑‒–—―]+", " ", value).split())


def words(value: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]+", value, re.UNICODE))


def spelling_limit(word: str) -> int:
    if not word.isalpha() or not all("LATIN" in unicodedata.name(c, "") for c in word):
        return 0
    return 2 if len(word) >= 8 else 1 if len(word) >= 4 else 0


def close_spelling(left: str, right: str, limit: int) -> bool:
    """Bounded optimal-string-alignment distance (including adjacent swaps)."""
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    older = previous
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            cost = min(current[-1] + 1, previous[j] + 1, previous[j - 1] + (a != b))
            if i > 1 and j > 1 and a == right[j - 2] and left[i - 2] == b:
                cost = min(cost, older[j - 2] + 1)
            current.append(cost)
        if min(current) > limit:
            return False
        older, previous = previous, current
    return previous[-1] <= limit


def load_aliases(path: Path) -> list[set[str]]:
    if not path.exists():
        return []
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"aliases"} or not isinstance(data["aliases"], list):
        raise ValueError(f"{path}: expected an aliases list of name lists")
    groups = []
    for group in data["aliases"]:
        if not isinstance(group, list) or len(group) < 2 or any(not isinstance(v, str) or not normalize(v) for v in group):
            raise ValueError(f"{path}: each alias group needs at least two nonempty names")
        groups.append({normalize(v) for v in group})
    return groups


def query_terms(query: str, aliases: list[set[str]]) -> tuple[set[str], set[str]]:
    """Return literal alternatives and words eligible for spelling expansion."""
    literals: set[str] = set()
    fuzzy: set[str] = set()
    normalized = normalize(query)
    for match in re.finditer(r'"([^"]+)"|(\S+)', normalized):
        phrase, word = match.groups()
        if phrase:
            literals.add(phrase)
        elif word:
            literals.add(word)
            fuzzy.add(word)
    # Full multiword aliases also work without requiring quotation marks.
    for group in aliases:
        if any(name in literals or re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", normalized) for name in group):
            literals.update(group)
    # Resolve overlapping groups transitively without making order significant.
    while True:
        expanded = set(literals)
        for group in aliases:
            if group & literals:
                expanded.update(group)
        if expanded == literals:
            break
        literals = expanded
    for term in literals:
        if " " not in term and term not in {m.group(1) for m in re.finditer(r'"([^"]+)"', normalized)}:
            fuzzy.add(term)
    return literals, fuzzy


def query_concepts(query: str, aliases: list[set[str]]) -> list[tuple[set[str], set[str]]]:
    """Split a query into required concepts and their alias alternatives.

    The first set in each tuple contains literal alternatives.  The second set
    contains unquoted, single-word alternatives eligible for spelling fallback.
    Multi-word aliases written without quotes are consumed as one concept.
    """
    normalized = normalize(query)
    units = [(match.group(1) or match.group(2), match.group(1) is not None)
             for match in re.finditer(r'"([^"]+)"|(\S+)', normalized)]

    def alternatives(value: str) -> set[str]:
        expanded = {value}
        while True:
            previous = set(expanded)
            for group in aliases:
                if group & expanded:
                    expanded.update(group)
            if expanded == previous:
                return expanded

    alias_names = set().union(*aliases) if aliases else set()
    concepts: list[tuple[set[str], set[str]]] = []
    index = 0
    while index < len(units):
        value, quoted = units[index]
        consumed = 1
        if not quoted:
            # Prefer the longest explicitly configured multi-word alias.
            for end in range(len(units), index + 1, -1):
                if any(is_quoted for _part, is_quoted in units[index:end]):
                    continue
                candidate = " ".join(part for part, _is_quoted in units[index:end])
                if candidate in alias_names:
                    value, consumed = candidate, end - index
                    break
        literal = alternatives(value)
        fuzzy = set() if quoted else {item for item in literal if " " not in item}
        concepts.append((literal, fuzzy))
        index += consumed
    return concepts
