from __future__ import annotations

import fcntl
import json
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

type JSON = str | int | float | bool | None | list[JSON] | dict[str, JSON]


@contextmanager
def locked(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    handle = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


@contextmanager
def unless_held(directory: Path) -> Iterator[bool]:
    directory.mkdir(parents=True, exist_ok=True)
    handle = os.open(directory, os.O_RDONLY)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            yield True
    finally:
        os.close(handle)


def part_of(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")


def written(part: Path, data: bytes, durable: bool) -> None:
    part.parent.mkdir(parents=True, exist_ok=True)
    with part.open("wb") as file:
        file.write(data)
        if durable:
            file.flush()
            os.fsync(file.fileno())


def staged(path: Path, data: bytes, durable: bool) -> None:
    part = part_of(path)
    written(part, data, durable)
    os.replace(part, path)


def claimed(path: Path, data: bytes) -> bool:
    part = part_of(path)
    written(part, data, True)
    try:
        os.link(part, path)
    except FileExistsError:
        return False
    else:
        return True
    finally:
        part.unlink()


def replace_text(path: Path, text: str) -> None:
    staged(path, text.encode(), False)


def record_of(line: str) -> dict[str, JSON] | None:
    try:
        data: JSON = json.loads(line)
    except json.JSONDecodeError:
        return None
    else:
        if isinstance(data, dict):
            return data
        else:
            return None


def records(path: Path) -> list[dict[str, JSON]]:
    found: list[dict[str, JSON]] = []
    if path.is_file():
        for line in path.read_text(errors="replace").split("\n")[:-1]:
            data = record_of(line)
            if data is not None:
                found.append(data)
    return found
