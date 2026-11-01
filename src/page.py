from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from store import replace_text

FENCE = re.compile(r"`+")
KEY = re.compile(r"^[a-z][a-z0-9 _]*$")
WORDS = re.compile(r"[^a-z0-9]+")
LONGEST = 80
CACHED = 200_000


class Unreadable(ValueError):
    pass


lock = threading.Lock()
cache: dict[Path, tuple[tuple[int, int], Page]] = {}


@dataclass(frozen=True)
class Page:
    title: str
    fields: tuple[tuple[str, str], ...]
    sections: tuple[tuple[str, str], ...]

    def one(self, key: str) -> str | None:
        for name, value in self.fields:
            if name == key:
                return value
        return None

    def need(self, key: str) -> str:
        value = self.one(key)
        if value is None:
            raise Unreadable(f"the page '{self.title}' has no field '{key}'")
        else:
            return value

    def every(self, key: str) -> list[str]:
        found: list[str] = []
        for name, value in self.fields:
            if name == key:
                found.append(value)
        return found

    def text(self, name: str) -> str | None:
        for heading, body in self.sections:
            if heading == name:
                return body
        return None

    def texts(self, name: str) -> list[str]:
        found: list[str] = []
        for heading, body in self.sections:
            if heading == name:
                found.append(body)
        return found

    def value(self, key: str) -> str:
        found = self.one(key)
        if found is None:
            found = self.text(key)
        if found is None:
            raise Unreadable(f"the page '{self.title}' has no field or section '{key}'")
        else:
            return found


class Builder:
    title: str
    fields: list[tuple[str, str]]
    sections: list[tuple[str, str]]

    def __init__(self, title: str) -> None:
        self.title = title
        self.fields = []
        self.sections = []

    def field(self, key: str, value: str) -> None:
        if "\n" in value or value != value.strip():
            self.sections.append((key, value))
        else:
            self.fields.append((key, value))

    def section(self, key: str, value: str) -> None:
        self.sections.append((key, value))

    def page(self) -> Page:
        return Page(self.title, tuple(self.fields), tuple(self.sections))


def fenced(text: str) -> list[str]:
    longest = 0
    for run in FENCE.findall(text):
        longest = max(longest, len(run))
    fence = "`" * max(3, longest + 1)
    return [fence, text, fence]


def single(text: str) -> str:
    return " ".join(text.split())


def rendered(page: Page) -> str:
    lines = [f"# {single(page.title)}", ""]
    for key, value in page.fields:
        if KEY.match(key) is None or "\n" in value or value != value.strip():
            raise ValueError(f"the field '{key}' of '{page.title}' cannot be one line: {value[:80]!r}")
        else:
            lines.append(f"{key}: {value}".rstrip())
    for heading, body in page.sections:
        lines.extend(["", f"## {single(heading)}", ""])
        lines.extend(fenced(body))
    return "\n".join(lines) + "\n"


def parsed(text: str) -> Page:
    lines = text.split("\n")
    if len(lines) == 0 or not lines[0].startswith("# "):
        raise Unreadable("a page starts with '# ' and its title")
    else:
        title = lines[0].removeprefix("# ")
        fields: list[tuple[str, str]] = []
        sections: list[tuple[str, str]] = []
        at = 1
        while at < len(lines) and not lines[at].startswith("## "):
            line = lines[at]
            if line != "":
                key, colon, value = line.partition(":")
                if colon == "" or KEY.match(key) is None:
                    raise Unreadable(f"line {at + 1} of '{title}' is not 'key: value': {line[:80]}")
                else:
                    fields.append((key, value.strip()))
            at += 1
        while at < len(lines):
            line = lines[at]
            if line.startswith("## "):
                heading = line.removeprefix("## ")
                start = at + 1
                while start < len(lines) and lines[start] == "":
                    start += 1
                if start >= len(lines) or FENCE.fullmatch(lines[start]) is None or len(lines[start]) < 3:
                    raise Unreadable(f"the section '{heading}' of '{title}' has no fenced text")
                else:
                    fence = lines[start]
                    end = start + 1
                    while end < len(lines) and lines[end] != fence:
                        end += 1
                    if end >= len(lines):
                        raise Unreadable(f"the section '{heading}' of '{title}' is not closed")
                    else:
                        sections.append((heading, "\n".join(lines[start + 1 : end])))
                        at = end + 1
            elif line == "":
                at += 1
            else:
                raise Unreadable(f"line {at + 1} of '{title}' is outside any section: {line[:80]}")
        return Page(title, tuple(fields), tuple(sections))


def read(path: Path) -> Page:
    info = path.stat()
    seen = (info.st_mtime_ns, info.st_size)
    with lock:
        held = cache.get(path)
    if held is not None and held[0] == seen:
        return held[1]
    else:
        try:
            found = parsed(path.read_text())
        except Unreadable as error:
            raise Unreadable(f"{path}: {error}") from None
        else:
            with lock:
                if len(cache) >= CACHED:
                    cache.clear()
                cache[path] = (seen, found)
            return found


def write(path: Path, page: Page) -> None:
    replace_text(path, rendered(page))


def pages(directory: Path) -> list[Path]:
    if directory.is_dir():
        return sorted(directory.glob("*.md"), key=numbered)
    else:
        return []


def numbered(path: Path) -> tuple[int, str]:
    head = path.stem.partition("-")[0]
    if head.isdigit():
        return int(head), path.stem
    else:
        return 0, path.stem


def slug(text: str) -> str:
    words = WORDS.sub("-", single(text).lower()).strip("-")
    if len(words) > LONGEST:
        words = words[:LONGEST].rpartition("-")[0]
    return words


def named(number: int, text: str) -> str:
    words = slug(text)
    if words == "":
        return f"{number}.md"
    else:
        return f"{number}-{words}.md"


def next_number(directory: Path) -> int:
    top = 0
    for path in pages(directory):
        top = max(top, numbered(path)[0])
    return top + 1


def moment(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def dated(stamp: float) -> str:
    return time.strftime("%Y-%m-%d-%H%M%S", time.localtime(stamp))


def flag(value: bool) -> str:
    if value:
        return "yes"
    else:
        return "no"


def flagged(text: str) -> bool:
    match text:
        case "yes":
            return True
        case "no":
            return False
        case _:
            raise Unreadable(f"'{text}' is not yes or no")


def number(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        raise Unreadable(f"'{text}' is not a whole number") from None


def real(text: str) -> float:
    try:
        return float(text.partition(" ")[0])
    except ValueError:
        raise Unreadable(f"'{text}' is not a number") from None


def when(stamp: float) -> str:
    return f"{stamp!r} ({moment(stamp)})"


def fresh_dir(parent: Path, stamp: float) -> Path:
    base = dated(stamp)
    parent.mkdir(parents=True, exist_ok=True)
    count = 1
    while True:
        name = base
        if count > 1:
            name = f"{base}-{count}"
        try:
            os.mkdir(parent / name)
        except FileExistsError:
            count += 1
        else:
            return parent / name


def items(path: Path, key: str) -> list[str]:
    if path.is_file():
        return read(path).every(key)
    else:
        return []


def write_items(path: Path, title: str, key: str, values: Sequence[str]) -> None:
    fields: list[tuple[str, str]] = []
    for value in values:
        fields.append((key, value))
    write(path, Page(title, tuple(fields), ()))
