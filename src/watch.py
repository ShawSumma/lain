from __future__ import annotations

import ctypes
import os
import select
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from tree import hidden, joined, shown

OPENED = 0x20
MODIFIED = 0x2
MOVED_FROM = 0x40
MOVED_TO = 0x80
CREATED = 0x100
DELETED = 0x200
OVERFLOWED = 0x4000
IGNORED = 0x8000
IS_DIRECTORY = 0x40000000
ONLY_DIRECTORY = 0x01000000
CHANGED = MODIFIED | MOVED_FROM | MOVED_TO | CREATED | DELETED
MASK = OPENED | CHANGED | ONLY_DIRECTORY
EVENT = struct.Struct("iIII")
CHUNK = 1 << 16


@dataclass(frozen=True)
class Touched:
    opened: frozenset[str]
    listed: frozenset[str]
    changed: frozenset[str]
    complete: bool


def library() -> ctypes.CDLL:
    lib = ctypes.CDLL(None)
    lib.inotify_init1.argtypes = [ctypes.c_int]
    lib.inotify_init1.restype = ctypes.c_int
    lib.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    lib.inotify_add_watch.restype = ctypes.c_int
    return lib


class Watch:
    root: Path
    lib: ctypes.CDLL
    fd: int
    wake: tuple[int, int]
    folders: dict[int, str]
    opened: set[str]
    listed: set[str]
    changed: set[str]
    existed: set[str]
    complete: bool
    thread: threading.Thread
    lock: threading.Lock

    def __init__(self, root: Path, lib: ctypes.CDLL, fd: int) -> None:
        self.root = root
        self.lib = lib
        self.fd = fd
        self.lock = threading.Lock()
        self.folders = {}
        self.opened = set()
        self.listed = set()
        self.changed = set()
        self.complete = True
        self.existed = set()
        for directory, names, files in os.walk(root):
            names[:] = shown(names)
            here = Path(directory).relative_to(root).as_posix()
            self.add(here)
            for name in files:
                self.existed.add(joined(here, name))
        self.wake = os.pipe()
        self.thread = threading.Thread(target=self.follow, daemon=True)
        self.thread.start()

    def add(self, directory: str) -> None:
        if (self.root / directory).is_dir():
            number = self.lib.inotify_add_watch(self.fd, os.fsencode(self.root / directory), MASK)
            if number < 0:
                self.complete = False
            else:
                self.folders[number] = directory

    def follow(self) -> None:
        while True:
            ready, _, _ = select.select([self.fd, self.wake[0]], [], [])
            self.drain()
            if self.wake[0] in ready:
                return

    def drain(self) -> None:
        while self.fd in select.select([self.fd], [], [], 0)[0]:
            self.take(os.read(self.fd, CHUNK))

    def take(self, data: bytes) -> None:
        offset = 0
        while offset < len(data):
            number, mask, _, size = EVENT.unpack_from(data, offset)
            name = os.fsdecode(data[offset + EVENT.size : offset + EVENT.size + size].rstrip(b"\0"))
            offset += EVENT.size + size
            if mask & OVERFLOWED:
                self.complete = False
            elif mask & IGNORED:
                self.folders.pop(number, None)
            elif number in self.folders:
                self.event(self.folders[number], name, mask)

    def event(self, directory: str, name: str, mask: int) -> None:
        path = directory
        if name != "":
            path = joined(directory, name)
        if hidden(path):
            pass
        elif mask & OPENED and mask & IS_DIRECTORY:
            self.listed.add(path)
        elif mask & OPENED:
            self.opened.add(path)
        elif mask & (CREATED | MOVED_TO) and mask & IS_DIRECTORY:
            self.grown(path)
        elif mask & CHANGED and not mask & IS_DIRECTORY:
            with self.lock:
                self.changed.add(path)

    def seen(self) -> frozenset[str]:
        with self.lock:
            return frozenset(self.changed)

    def grown(self, directory: str) -> None:
        if (self.root / directory).is_dir():
            self.add(directory)
            with os.scandir(self.root / directory) as entries:
                for entry in entries:
                    path = joined(directory, entry.name)
                    if hidden(path):
                        pass
                    elif entry.is_dir(follow_symlinks=False):
                        self.grown(path)
                    else:
                        with self.lock:
                            self.changed.add(path)

    def finish(self) -> Touched:
        os.write(self.wake[1], b"x")
        self.thread.join()
        os.close(self.fd)
        os.close(self.wake[0])
        os.close(self.wake[1])
        lasting: set[str] = set()
        for path in self.changed:
            if path in self.existed or (self.root / path).exists():
                lasting.add(path)
        return Touched(frozenset(self.opened), frozenset(self.listed), frozenset(lasting), self.complete)


def watch(root: Path) -> Watch | None:
    if sys.platform != "linux":
        return None
    else:
        lib = library()
        fd = lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            return None
        else:
            return Watch(root, lib, fd)
