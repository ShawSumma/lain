from __future__ import annotations

import math
import re
from collections.abc import Sequence

WORD = re.compile(r"\w+")


def words(text: str) -> set[str]:
    found: set[str] = set()
    for word in WORD.findall(text.casefold()):
        found.add(word)
    return found


def scores(query: str, documents: Sequence[set[str]]) -> list[float]:
    counts: dict[str, int] = {}
    for document in documents:
        for word in document:
            counts[word] = counts.get(word, 0) + 1
    asked = words(query)
    found: list[float] = []
    for document in documents:
        score = 0.0
        for word in asked & document:
            score += math.log(len(documents) / counts[word])
        if len(document) > 0:
            score = score / math.sqrt(len(document))
        found.append(score)
    return found


def unmatched(query: str, documents: Sequence[set[str]]) -> list[str]:
    seen: set[str] = set()
    for document in documents:
        seen.update(document)
    missing: list[str] = []
    for word in sorted(words(query)):
        if word not in seen:
            missing.append(word)
    return missing
