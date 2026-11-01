from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from page import Page, read, write
from places import BUILDS, CHECKOUT, INSTALL

PARTS = ("src", "prompts", "web")
SUFFIXES = (".py", ".md", ".html", ".js")
CURRENT = "current.md"
SOURCE = "source.md"
HOME = INSTALL / BUILDS


@dataclass(frozen=True)
class Broken:
    reason: str


def sources(checkout: Path) -> list[Path]:
    found: list[Path] = []
    for part in PARTS:
        for path in sorted((checkout / part).iterdir()):
            if path.is_file() and path.suffix in SUFFIXES:
                found.append(path.relative_to(checkout))
    return found


def digest_of(checkout: Path, paths: list[Path]) -> str:
    total = hashlib.sha256()
    for path in paths:
        data = (checkout / path).read_bytes()
        total.update(f"{path.as_posix()}\0{len(data)}\0".encode())
        total.update(data)
    return total.hexdigest()[:16]


def broken(checkout: Path, paths: list[Path]) -> Broken | None:
    for path in paths:
        if path.suffix == ".py":
            try:
                compile((checkout / path).read_bytes(), str(path), "exec", dont_inherit=True)
            except SyntaxError as error:
                return Broken(f"{path}:{error.lineno}: {error.msg}")
    return None


def current() -> Path | None:
    marker = HOME / CURRENT
    if marker.is_file():
        return HOME / read(marker).need("build")
    else:
        return None


def digest_in(target: Path) -> str | None:
    if (target / SOURCE).is_file():
        return read(target / SOURCE).one("digest")
    else:
        return None


def changed_since(target: Path) -> bool:
    return digest_in(target) != digest_of(CHECKOUT, sources(CHECKOUT))


def numbers() -> list[int]:
    found: list[int] = []
    if HOME.is_dir():
        for path in HOME.iterdir():
            if path.is_dir() and path.name.isdigit():
                found.append(int(path.name))
    return sorted(found)


def made(digest: str, paths: list[Path]) -> Path:
    for each in numbers():
        if digest_in(HOME / str(each)) == digest:
            return HOME / str(each)
    target = HOME / str(max(numbers(), default=0) + 1)
    staging = HOME / f"staging-{os.getpid()}"
    for path in paths:
        (staging / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(CHECKOUT / path, staging / path)
    write(staging / SOURCE, Page(f"build {target.name} of {CHECKOUT}", (("digest", digest),), ()))
    staging.rename(target)
    return target


def chosen(target: Path) -> None:
    write(HOME / CURRENT, Page("the build lain runs", (("build", target.name),), ()))


def build() -> Path | Broken:
    paths = sources(CHECKOUT)
    wrong = broken(CHECKOUT, paths)
    if wrong is not None:
        return wrong
    else:
        HOME.mkdir(parents=True, exist_ok=True)
        target = made(digest_of(CHECKOUT, paths), paths)
        chosen(target)
        return target


def prune(kept: tuple[Path, ...]) -> None:
    for path in HOME.iterdir():
        if path.is_dir() and path not in kept:
            shutil.rmtree(path)
