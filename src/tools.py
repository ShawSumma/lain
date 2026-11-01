from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from container import Container
from finder import Finder
from history import Call, Result
from prompts import message
from tree import (
    Change,
    Seen,
    changes_between,
    content,
    diff,
    digest,
    directories_under,
    excluded,
    file_diff,
    files_under,
    hashed,
    listing_digest,
    normal,
    text_at,
)
from watch import Touched, watch

DEPENDENT = ("list_dir", "read_file", "search", "find", "run", "claude")


@dataclass(frozen=True)
class Outcome:
    text: str
    ok: bool
    patch: tuple[Change, ...]
    reads: tuple[Seen, ...] | None
    seconds: float


class PathArgs(TypedDict):
    path: str


class SearchArgs(TypedDict):
    pattern: str
    paths: list[str]


class FindArgs(TypedDict):
    question: str
    paths: list[str]


class WriteArgs(TypedDict):
    path: str
    content: str


class EditArgs(TypedDict):
    path: str
    old: str
    new: str


class RunArgs(TypedDict):
    command: str


def done(text: str, reads: Sequence[Seen] = ()) -> Outcome:
    return Outcome(text, True, (), tuple(reads), 0.0)


def failed(text: str, reads: Sequence[Seen] = ()) -> Outcome:
    return Outcome(text, False, (), tuple(reads), 0.0)


def changed(text: str, patch: tuple[Change, ...]) -> Outcome:
    return Outcome(text, True, patch, (), 0.0)


def line_numbers(text: str, needle: str) -> list[int]:
    numbers: list[int] = []
    index = text.find(needle)
    while index != -1:
        numbers.append(text.count("\n", 0, index) + 1)
        index = text.find(needle, index + 1)
    return numbers


def first_line(text: str) -> str | None:
    for line in text.splitlines():
        if line.strip() != "":
            return line.strip()
    return None


def joined_numbers(numbers: list[int]) -> str:
    shown: list[str] = []
    for number in numbers:
        shown.append(str(number))
    return ", ".join(shown)


def absent(path: str, text: str, old: str) -> Outcome:
    first = first_line(old)
    numbers: list[int] = []
    if first is not None:
        numbers = line_numbers(text, first)
    if len(numbers) == 0:
        return failed(message("edit_absent", path=path))
    else:
        return failed(message("edit_absent_near", path=path, lines=joined_numbers(numbers)))


def matching_lines(path: str, text: str, pattern: re.Pattern[str]) -> list[str]:
    found: list[str] = []
    number = 0
    for line in text.splitlines():
        number += 1
        if pattern.search(line) is not None:
            found.append(f"{path}:{number}: {line.strip()}")
    return found


class Toolbox:
    root: Path
    commands: Mapping[str, tuple[str, ...]]
    writable: tuple[str, ...]
    finder: Finder
    base: Path
    container: Container | None

    def __init__(
        self,
        root: Path,
        commands: Mapping[str, tuple[str, ...]],
        writable: tuple[str, ...],
        finder: Finder,
        base: Path,
        container: Container | None,
    ) -> None:
        self.root = root
        self.commands = commands
        self.writable = writable
        self.finder = finder
        self.base = base
        self.container = container

    def inside(self, raw: str) -> str | None:
        path = normal(raw)
        if path is not None and (self.root / path).resolve().is_relative_to(self.root.resolve()):
            return path
        else:
            return None

    def tracked(self, path: str) -> bool:
        return path in self.writable or (self.base / path).exists() or not (self.root / path).exists()

    def file_seen(self, path: str) -> Seen:
        return Seen(path, "file", digest(self.root, path))

    def listing_seen(self, path: str) -> Seen:
        return Seen(path, "listing", listing_digest(self.base, path))

    def tree_digest(self, path: str) -> str:
        parts: list[str] = []
        for directory in directories_under(self.base, path):
            parts.append(f"{directory}/\0{listing_digest(self.base, directory)}")
        for file in files_under(self.root, path):
            if self.tracked(file):
                parts.append(f"{file}\0{digest(self.root, file)}")
        return hashed("\n".join(parts).encode())

    def tree_seen(self, path: str) -> Seen:
        return Seen(path, "tree", self.tree_digest(path))

    def bound(self) -> Seen:
        return Seen(".", "bound", self.tree_digest("."))

    def now(self, seen: Seen) -> str | None:
        match seen.kind:
            case "file":
                return digest(self.root, seen.path)
            case "listing":
                return listing_digest(self.base, seen.path)
            case "tree" | "bound":
                return self.tree_digest(seen.path)

    def current(self, result: Result) -> bool:
        if result.tool not in DEPENDENT or result.reads is None:
            return False
        else:
            for seen in result.reads:
                if self.now(seen) != seen.digest:
                    return False
            return True

    def dispatch(self, call: Call) -> Outcome:
        match call.tool:
            case "list_dir":
                return self.list_dir(json.loads(call.arguments))
            case "read_file":
                return self.read_file(json.loads(call.arguments))
            case "search":
                return self.search(json.loads(call.arguments))
            case "find":
                return self.find(json.loads(call.arguments))
            case "changes":
                return self.changes()
            case "write_file":
                return self.write_file(json.loads(call.arguments))
            case "edit_file":
                return self.edit_file(json.loads(call.arguments))
            case "delete_file":
                return self.delete_file(json.loads(call.arguments))
            case "run":
                return self.run(json.loads(call.arguments))
            case _:
                return failed(message("no_such_tool", name=call.tool))

    def list_dir(self, args: PathArgs) -> Outcome:
        path = self.inside(args["path"])
        if path is None:
            return failed(message("outside", path=args["path"]))
        elif not (self.root / path).is_dir():
            return failed(message("not_directory", path=path), (self.listing_seen(path),))
        else:
            return self.listing(path)

    def listing(self, path: str) -> Outcome:
        reads: list[Seen] = [self.listing_seen(path)]
        directories: list[str] = []
        files: list[str] = []
        for child in sorted((self.root / path).iterdir()):
            relative = child.relative_to(self.root).as_posix()
            if excluded(child.name):
                pass
            elif child.is_dir():
                directories.append(message("directory_entry", name=child.name))
            elif child.is_file():
                files.append(message("file_entry", name=child.name, lines=str(content(child, False).lines)))
                if self.tracked(relative):
                    reads.append(self.file_seen(relative))
            else:
                files.append(message("other_entry", name=child.name))
        if len(directories) + len(files) == 0:
            return done(message("empty_directory"), reads)
        else:
            return done("\n".join(directories + files), reads)

    def read_file(self, args: PathArgs) -> Outcome:
        path = self.inside(args["path"])
        if path is None:
            return failed(message("outside", path=args["path"]))
        else:
            reads: list[Seen] = []
            if self.tracked(path):
                reads.append(self.file_seen(path))
            if (self.root / path).is_dir():
                return failed(message("directory", path=path), reads)
            elif not (self.root / path).is_file():
                return failed(message("missing", path=path), reads)
            else:
                return done((self.root / path).read_text(errors="replace"), reads)

    def search(self, args: SearchArgs) -> Outcome:
        try:
            pattern = re.compile(args["pattern"])
        except re.error as error:
            return failed(message("search_pattern", pattern=args["pattern"], error=str(error)))
        else:
            return self.searched(pattern, args["paths"])

    def searched(self, pattern: re.Pattern[str], paths: Sequence[str]) -> Outcome:
        files: list[str] = []
        reads: list[Seen] = []
        for raw in paths:
            path = self.inside(raw)
            if path is None:
                return failed(message("outside", path=raw), reads)
            else:
                reads.append(self.tree_seen(path))
                if not (self.root / path).exists():
                    return failed(message("missing", path=path), reads)
                else:
                    files.extend(files_under(self.root, path))
        lines: list[str] = []
        for path in files:
            lines.extend(matching_lines(path, (self.root / path).read_text(errors="replace"), pattern))
        if len(lines) == 0:
            return done(message("search_none"), reads)
        else:
            return done("\n".join(lines), reads)

    def find(self, args: FindArgs) -> Outcome:
        reads: list[Seen] = []
        for raw in args["paths"]:
            path = self.inside(raw)
            if path is not None:
                reads.append(self.tree_seen(path))
        return done(self.finder.find(self.root, args["question"], args["paths"]), reads)

    def changes(self) -> Outcome:
        found = changes_between(self.base, self.root, self.writable)
        if len(found) == 0:
            return done(message("no_changes"))
        else:
            return done(diff(self.base, found))

    def refused(self, path: str) -> Outcome | None:
        if path in self.writable:
            return None
        else:
            return failed(message("scope_denied", path=path, files=", ".join(self.writable)))

    def write_file(self, args: WriteArgs) -> Outcome:
        path = args["path"]
        refusal = self.refused(path)
        if refusal is not None:
            return refusal
        elif (self.root / path).is_dir():
            return failed(message("directory", path=path))
        else:
            before = text_at(self.root, path)
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(args["content"])
            return self.written(path, before, args["content"])

    def written(self, path: str, before: str | None, after: str) -> Outcome:
        change = (Change(path, after),)
        if before is None:
            return changed(message("created", path=path), change)
        elif before == after:
            return done(message("unchanged", path=path))
        else:
            return changed(file_diff(path, before, after), change)

    def edit_file(self, args: EditArgs) -> Outcome:
        path = args["path"]
        refusal = self.refused(path)
        if refusal is not None:
            return refusal
        elif not (self.root / path).is_file():
            return failed(message("missing", path=path))
        else:
            return self.replace(path, (self.root / path).read_text(errors="replace"), args["old"], args["new"])

    def replace(self, path: str, text: str, old: str, new: str) -> Outcome:
        count = text.count(old)
        if count == 1:
            after = text.replace(old, new, 1)
            (self.root / path).write_text(after)
            return self.written(path, text, after)
        elif count > 1:
            lines = joined_numbers(line_numbers(text, old))
            return failed(message("edit_ambiguous", count=str(count), path=path, lines=lines))
        else:
            return absent(path, text, old)

    def delete_file(self, args: PathArgs) -> Outcome:
        path = args["path"]
        refusal = self.refused(path)
        if refusal is not None:
            return refusal
        elif not (self.root / path).is_file():
            return failed(message("missing", path=path))
        else:
            (self.root / path).unlink()
            return changed(message("deleted", path=path), (Change(path, None),))

    def run(self, args: RunArgs) -> Outcome:
        command: list[str] = list(self.commands[args["command"]])
        if self.container is not None:
            command = self.container.argv(self.root, command)
        before: dict[str, str | None] = {}
        for path in self.writable:
            before[path] = text_at(self.root, path)
        bound = self.bound()
        watcher = watch(self.root)
        reads: tuple[Seen, ...] = ()
        if watcher is None:
            reads = (Seen(".", "tree", bound.digest),)
        start = time.monotonic()
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        completed = subprocess.run(command, cwd=self.root, capture_output=True, text=True, errors="replace", env=env)
        seconds = time.monotonic() - start
        lines = [message("ran", code=str(completed.returncode))]
        if watcher is not None:
            touched = watcher.finish()
            lines.extend(self.touched_lines(touched))
            if touched.complete:
                reads = self.run_reads(touched) + (bound,)
            else:
                reads = (Seen(".", "tree", bound.digest),)
        patch: list[Change] = []
        for path, old in before.items():
            new = text_at(self.root, path)
            if new != old:
                patch.append(Change(path, new))
        output = f"{completed.stdout}{completed.stderr}".strip()
        if output != "":
            lines.append(output)
        return Outcome("\n".join(lines), completed.returncode == 0, tuple(patch), reads, seconds)

    def touched_lines(self, touched: Touched) -> list[str]:
        lines: list[str] = []
        read = sorted(touched.opened - touched.changed)
        if len(read) > 0:
            lines.append(message("ran_read", files=", ".join(read)))
        kept: list[str] = []
        outside: list[str] = []
        for path in sorted(touched.changed):
            if path in self.writable or len(self.writable) == 0:
                kept.append(path)
            else:
                outside.append(path)
        if len(kept) > 0:
            lines.append(message("ran_changed", files=", ".join(kept)))
        if len(outside) > 0:
            lines.append(message("ran_outside", files=", ".join(outside)))
        return lines

    def run_reads(self, touched: Touched) -> tuple[Seen, ...]:
        found: list[Seen] = []
        for path in sorted(touched.opened - touched.changed):
            if self.tracked(path):
                found.append(self.file_seen(path))
        for path in sorted(touched.listed):
            if (self.base / path).is_dir():
                found.append(self.listing_seen(path))
        return tuple(found)
