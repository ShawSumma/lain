from __future__ import annotations

import difflib
import hashlib
import os
import posixpath
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from places import LAIN

type SeenKind = Literal["file", "listing", "tree", "bound"]

EXCLUDED = (".git", LAIN)
CHUNK = 1 << 20
ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "f": "\f", "r": "\r", "v": "\v"}


@dataclass(frozen=True)
class Change:
    path: str
    content: str | None


@dataclass(frozen=True)
class Seen:
    path: str
    kind: SeenKind
    digest: str | None


@dataclass(frozen=True)
class Section:
    old: str | None
    new: str | None
    renamed: bool


@dataclass(frozen=True)
class Content:
    digest: str
    lines: int
    data: bytes | None


def excluded(name: str) -> bool:
    return name in EXCLUDED


def hidden_names(directory: str, names: list[str]) -> list[str]:
    found: list[str] = []
    for name in names:
        if excluded(name):
            found.append(name)
    return found


def hidden(path: str) -> bool:
    for part in path.split("/"):
        if excluded(part):
            return True
    return False


def shown(names: Sequence[str]) -> list[str]:
    kept: list[str] = []
    for name in names:
        if not excluded(name):
            kept.append(name)
    return sorted(kept)


def joined(directory: str, name: str) -> str:
    if directory == ".":
        return name
    else:
        return f"{directory}/{name}"


def normal(raw: str) -> str | None:
    clean = posixpath.normpath(raw.strip())
    if "\x00" in raw or clean.startswith("/") or clean == ".." or clean.startswith("../"):
        return None
    elif hidden(clean):
        return None
    else:
        return clean


def files_under(root: Path, path: str) -> list[str]:
    found: list[str] = []
    target = root / path
    if target.is_file():
        found.append(path)
    elif target.is_dir():
        for directory, names, files in os.walk(target):
            names[:] = shown(names)
            for name in shown(files):
                found.append((Path(directory) / name).relative_to(root).as_posix())
    return found


def directories_under(root: Path, path: str) -> list[str]:
    found: list[str] = []
    if (root / path).is_dir():
        for directory, names, _ in os.walk(root / path):
            names[:] = shown(names)
            found.append(Path(directory).relative_to(root).as_posix())
    return found


def hashed(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def content(target: Path, keep: bool) -> Content:
    hasher = hashlib.sha256()
    lines = 0
    kept: list[bytes] = []
    with target.open("rb") as file:
        chunk = file.read(CHUNK)
        while chunk != b"":
            hasher.update(chunk)
            lines += chunk.count(b"\n")
            if keep:
                kept.append(chunk)
            chunk = file.read(CHUNK)
    if keep:
        return Content(hasher.hexdigest()[:16], lines, b"".join(kept))
    else:
        return Content(hasher.hexdigest()[:16], lines, None)


def digest(root: Path, path: str) -> str | None:
    target = root / path
    if target.is_file():
        return content(target, False).digest
    else:
        return None


def listing_digest(root: Path, path: str) -> str | None:
    target = root / path
    if not target.is_dir():
        return None
    else:
        names: list[str] = []
        for child in target.iterdir():
            if excluded(child.name):
                pass
            elif child.is_dir():
                names.append(f"{child.name}/")
            else:
                names.append(child.name)
        return hashed("\n".join(sorted(names)).encode())


def stamp(root: Path) -> str:
    parts: list[str] = []
    for directory, names, files in os.walk(root):
        names[:] = shown(names)
        parts.append(Path(directory).relative_to(root).as_posix())
        for name in shown(files):
            info = os.lstat(Path(directory) / name)
            parts.append(f"{name}\0{info.st_size}\0{info.st_mtime_ns}")
    return hashed("\n".join(parts).encode())


def text_at(root: Path, path: str) -> str | None:
    target = root / path
    if target.is_file():
        return target.read_text(errors="replace")
    else:
        return None


def copy_project(source: Path, root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)
    shutil.copytree(source, root, symlinks=True, ignore=hidden_names)
    (root / ".git").write_text("gitdir: /dev/null\n")


def apply(root: Path, changes: Sequence[Change]) -> None:
    for change in changes:
        target = root / change.path
        if change.content is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(change.content)
        elif target.is_file():
            target.unlink()


def changes_between(before: Path, after: Path, paths: Sequence[str]) -> tuple[Change, ...]:
    found: list[Change] = []
    for path in paths:
        new = text_at(after, path)
        if text_at(before, path) != new:
            found.append(Change(path, new))
    return tuple(found)


def file_diff(path: str, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(lines)


def quoted(text: str) -> tuple[str, str]:
    data = bytearray()
    index = 1
    while text[index] != '"':
        if text[index] == "\\" and text[index + 1] in "01234567":
            data.append(int(text[index + 1 : index + 4], 8))
            index += 4
        elif text[index] == "\\":
            data.extend(ESCAPES[text[index + 1]].encode())
            index += 2
        else:
            data.extend(text[index].encode())
            index += 1
    return data.decode(errors="replace"), text[index + 1 :]


def unquoted(text: str) -> str:
    if text.startswith('"'):
        return quoted(text)[0]
    else:
        return text


def header_paths(header: str) -> tuple[str, str]:
    rest = header.removeprefix("diff --git ")
    if rest.startswith('"'):
        first, rest = quoted(rest)
        second = unquoted(rest.strip())
    elif ' "b/' in rest:
        first, _, rest = rest.partition(' "b/')
        second = unquoted(f'"b/{rest}')
    else:
        first, _, rest = rest.partition(" b/")
        second = f"b/{rest}"
    return first.removeprefix("a/"), second.removeprefix("b/")


def section(text: str) -> Section:
    lines = text.splitlines()
    old: str | None
    new: str | None
    old, new = header_paths(lines[0])
    renamed = False
    for line in lines[1:]:
        if line.startswith("@@"):
            break
        elif line.startswith("new file mode"):
            old = None
        elif line.startswith("deleted file mode"):
            new = None
        elif line.startswith("rename from "):
            old = unquoted(line.removeprefix("rename from "))
            renamed = True
        elif line.startswith("rename to "):
            new = unquoted(line.removeprefix("rename to "))
    return Section(old, new, renamed)


def section_paths(text: str) -> list[str]:
    part = section(text)
    found: list[str] = []
    for path in (part.old, part.new):
        if path is not None and path not in found:
            found.append(path)
    return found


def diff(before: Path, changes: Sequence[Change]) -> str:
    parts: list[str] = []
    for change in changes:
        old = text_at(before, change.path)
        new = change.content
        if old is None:
            old = ""
        if new is None:
            new = ""
        parts.append(file_diff(change.path, old, new))
    return "".join(parts)
