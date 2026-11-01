from __future__ import annotations

import os
import re
from collections.abc import Sequence
from functools import cache

from keys import KEYS

SHAPES = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----"
    r"|\bsk-[A-Za-z0-9_-]{16,}"
    r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
    r"|\bgithub_pat_[A-Za-z0-9_]{40,}"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\bAIza[0-9A-Za-z_-]{35}"
    r"|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
    re.DOTALL,
)
ASSIGNED = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|secret|token|passwd|password)[\w-]*)(\s*[:=]\s*['\"]?)"
    r"((?=[A-Za-z0-9_+/=-]*[0-9])(?=[A-Za-z0-9_+/=-]*[A-Za-z])[A-Za-z0-9_+/=-]{16,})(?![\w.(])"
)
SECRETISH = re.compile(r"(?i)key|token|secret|passw|credential|auth")
SHOWN = "[redacted]"
SHORTEST = 8


@cache
def known() -> tuple[str, ...]:
    found: set[str] = set()
    for key in KEYS:
        if len(key) >= SHORTEST:
            found.add(key)
    for name, value in os.environ.items():
        if SECRETISH.search(name) is not None and len(value) >= SHORTEST:
            found.add(value)
    return tuple(sorted(found, key=len, reverse=True))


def redacted(text: str) -> str:
    for value in known():
        text = text.replace(value, SHOWN)
    text = SHAPES.sub(SHOWN, text)
    return ASSIGNED.sub(lambda found: f"{found.group(1)}{found.group(2)}{SHOWN}", text)


def redacted_each(texts: Sequence[str]) -> tuple[str, ...]:
    found: list[str] = []
    for text in texts:
        found.append(redacted(text))
    return tuple(found)
