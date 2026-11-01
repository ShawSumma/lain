from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATTERNS = ("prompts/**/*.md", "src/**/*.py", "web/*.html", "web/*.js")
KEY = re.compile(r'^(API_KEY|OPENROUTER_KEY) = ".*"$', re.MULTILINE)


def masked(source: Path) -> str:
    return KEY.sub(r'\1 = "..."', source.read_text().strip())


def main() -> int:
    sources: list[Path] = []
    for pattern in PATTERNS:
        sources.extend(HERE.glob(pattern))
    sections: list[str] = []
    for source in sorted(sources):
        sections.append(f"### file: {source.relative_to(HERE).as_posix()}\n{masked(source)}\n")
    (HERE / "export.txt").write_text("\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
