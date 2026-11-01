from __future__ import annotations

from functools import cache
from pathlib import Path
from string import Template

PROMPTS = Path(__file__).parent.parent / "prompts"


@cache
def sections(name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    key: str | None = None
    lines: list[str] = []
    for line in (PROMPTS / f"{name}.md").read_text().splitlines():
        if line.startswith("## "):
            if key is not None:
                found[key] = "\n".join(lines).strip("\n")
            key = line.removeprefix("## ").strip()
            lines = []
        else:
            lines.append(line)
    if key is not None:
        found[key] = "\n".join(lines).strip("\n")
    return found


def message(key: str, /, **values: str) -> str:
    return Template(sections("messages")[key]).safe_substitute(values)


def tool_text(key: str) -> str:
    return sections("tools")[key]


def prompt(name: str, /, **values: str) -> str:
    return Template((PROMPTS / f"{name}.md").read_text().strip()).safe_substitute(values)
