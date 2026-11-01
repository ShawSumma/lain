from __future__ import annotations

import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

from bugs import noticed
from config import Settings
from extract import Access, Explored, Extractor, cut
from memory import Memory, Store, Watch, covers, lead, scoped
from model import Failure, Usage
from page import Builder, Page, Unreadable, pages, read, real, slug, when, write
from places import Places, places
from prompts import message, prompt
from rank import WORD, scores, unmatched, words
from spans import chunk, here, span
from skeleton import LARGEST, MAP, ShapeData, Skeleton, SymbolData, SymbolKind, depth_of, parent_of, skeleton
from spec import DESCRIBE, DescribeArgs
from stale import names_in, staleness
from tree import digest, hashed

type PartKind = Literal["region", "directory", "file"]
type LocatedKind = Literal["region", "file"]

REGIONS = "regions"
ROOM = 40_000
README_ROOM = 4_000
SYMBOLS = 40
OTHERS = 40
LINKED = 12
HELD = 12
FILENAME = re.compile(r"[\w./-]+\.(?:py|kt|kts|java|md|gradle|json|js|mjs|ts|tsx|toml|txt|ya?ml|xml|html|css|sh|temper|rs|go|c|h|cpp|hpp|cs|lua)\b")
CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+")


class NoteData(TypedDict):
    path: str
    key: str
    summary: str
    text: str
    unchecked: list[str]
    files: dict[str, str]
    time: float


class NoteShown(TypedDict):
    path: str
    summary: str
    text: str
    fresh: bool
    unchecked: list[str]
    time: float


class PartData(TypedDict):
    path: str
    kind: PartKind
    summary: str
    lines: int
    files: int


class CountData(TypedDict):
    path: str
    count: int


class SymbolShown(TypedDict):
    name: str
    kind: SymbolKind
    line: int
    signature: str


class ModuleData(TypedDict):
    path: str
    manifests: list[str]
    depends: list[str]
    used_by: list[str]


@dataclass(frozen=True)
class Pending:
    path: str
    key: str
    brief: str
    files: dict[str, str]


@dataclass(frozen=True)
class Built:
    written: int
    current: int
    failed: tuple[str, ...]


@dataclass(frozen=True)
class Located:
    kind: LocatedKind
    path: str
    title: str
    detail: str
    score: float


def notes_home(found: Places) -> Path:
    return found.home / MAP / REGIONS


def note_name(path: str) -> str:
    if path == ".":
        return "project.md"
    else:
        return f"{slug(path)}.md"


def note_page(note: NoteData) -> Page:
    built = Builder(f"{region_name(note['path'])}: {note['summary']}")
    built.field("region", note["path"])
    built.field("key", note["key"])
    built.field("time", when(note["time"]))
    for name in note["unchecked"]:
        built.field("unchecked", name)
    for path, seen in sorted(note["files"].items()):
        built.field("file", f"{seen} {path}")
    built.section("summary", note["summary"])
    built.section("text", note["text"])
    return built.page()


def note_of(page: Page) -> NoteData:
    files: dict[str, str] = {}
    for line in page.every("file"):
        seen, _, path = line.partition(" ")
        files[path] = seen
    return {
        "path": page.need("region"),
        "key": page.need("key"),
        "summary": page.value("summary"),
        "text": page.value("text"),
        "unchecked": page.every("unchecked"),
        "files": files,
        "time": real(page.need("time")),
    }


def notes_in(found: Places) -> dict[str, NoteData]:
    notes: dict[str, NoteData] = {}
    for path in pages(notes_home(found)):
        try:
            note = note_of(read(path))
        except Unreadable as error:
            noticed("map", str(found.repo.root), f"the map page {path.name} cannot be read, so it is left out: {error}", str(path))
        else:
            notes[note["path"]] = note
    return notes


def interface(shape: ShapeData) -> str:
    lines: list[str] = []
    for symbol in shape["symbols"]:
        lines.append(f"{symbol['kind']} {symbol['name']} {symbol['signature']}")
    return hashed("\n".join(lines).encode())


def readme(path: str) -> bool:
    return PurePosixPath(path).name.lower().startswith("readme")


def region_name(directory: str) -> str:
    if directory == ".":
        return message("map_project")
    else:
        return directory


def parts_of(text: str) -> set[str]:
    found = words(text)
    for token in WORD.findall(text):
        for part in CAMEL.findall(token):
            found.add(part.casefold())
    return found


def at(path: str, files: Sequence[str]) -> bool:
    if path != "." and not path.endswith("/"):
        return True
    else:
        for each in files:
            if each == path or (scoped(each) and covers(each, path)) or (not scoped(each) and parent_of(each) == path):
                return True
        return False


def holding(store: Store, watch: Watch, path: str) -> list[Memory]:
    found: list[Memory] = []
    for memory in store.relevant([path], watch):
        here = at(path, store.current_files(memory)) or (path == "." and memory.basis == "user")
        if memory.kind != "option" and here:
            found.append(memory)
    return store.ranked(found, path)


def held_in(store: Store, watch: Watch, directory: str) -> list[Memory]:
    found: list[Memory] = []
    if directory != ".":
        for memory in holding(store, watch, directory):
            if (memory.kind == "decision" or memory.basis == "user") and len(found) < HELD:
                found.append(memory)
    return found


def readable(root: Path, path: str) -> str | None:
    try:
        if os.stat(root / path).st_size > LARGEST:
            return None
        else:
            return (root / path).read_text(errors="replace")
    except OSError:
        return None


def mentioned(root: Path, paths: Sequence[str], names: set[str]) -> set[str]:
    found: set[str] = set()
    if len(names) > 0:
        ordered = sorted(names, key=len, reverse=True)
        escaped: list[str] = []
        for name in ordered:
            escaped.append(re.escape(name))
        pattern = re.compile(rf"\b({'|'.join(escaped)})\b")
        for path in paths:
            if not found >= names:
                found.update(pattern.findall(readable(root, path) or ""))
    return found


def listing(items: Sequence[str], limit: int) -> str:
    if len(items) == 0:
        return message("none")
    elif len(items) > limit:
        return "\n".join([*items[:limit], message("map_more", count=str(len(items) - limit))])
    else:
        return "\n".join(items)


def summary_of(note: NoteData | None) -> str:
    if note is None:
        return ""
    else:
        return note["summary"]


def text_digest(note: NoteData | None) -> str:
    if note is None:
        return "none"
    else:
        return hashed(note["text"].encode())


def counted_lines(counts: Sequence[CountData]) -> list[str]:
    lines: list[str] = []
    for each in counts:
        lines.append(f"- {each['path']} ({each['count']})")
    return lines


def singles(paths: Sequence[str]) -> list[CountData]:
    found: list[CountData] = []
    for path in paths:
        found.append({"path": path, "count": 1})
    return found


def symbol_lines(symbols: Sequence[SymbolData], full: bool) -> list[str]:
    lines: list[str] = []
    for each in symbols[:SYMBOLS]:
        if full:
            lines.append(f"    {each['kind']} {each['name']}: {each['signature']}")
        else:
            lines.append(f"    {each['kind']} {each['name']}")
    if len(symbols) > SYMBOLS:
        lines.append(f"    {message('map_more', count=str(len(symbols) - SYMBOLS))}")
    return lines


def outermost(symbols: Sequence[SymbolData]) -> list[SymbolData]:
    found: list[SymbolData] = []
    for each in symbols:
        if "." not in each["name"]:
            found.append(each)
    return found


class Atlas:
    home: Path
    skeleton: Skeleton
    notes: dict[str, NoteData]
    described: set[str]
    below: dict[str, list[str]]
    index: list[tuple[Located, set[str]]] | None

    def __init__(self, at: Places, found: Skeleton) -> None:
        self.home = notes_home(at)
        self.skeleton = found
        self.notes = notes_in(at)
        self.described = set()
        self.below = {}
        self.index = None
        tops: dict[str, list[str]] = {}
        for directory in found.directories():
            beneath: list[str] = []
            own = False
            for child in found.children[directory]:
                if child.endswith("/"):
                    beneath.extend(tops[child])
                elif found.sources[child].language is not None:
                    own = True
            self.below[directory] = beneath
            if directory == "." or directory in found.modules or own or len(beneath) >= 2:
                self.described.add(directory)
                tops[directory] = [directory]
            else:
                tops[directory] = beneath

    def regions(self) -> list[str]:
        return sorted(self.described, key=lambda path: (-depth_of(path), path))

    def written(self) -> int:
        count = 0
        for region in self.described:
            if region in self.notes:
                count += 1
        return count

    def gone(self, path: str) -> bool:
        if path == "." or path.endswith("/"):
            return path not in self.skeleton.children
        else:
            return path not in self.skeleton.sources

    def located(self, path: str) -> str:
        clean = path.strip().removeprefix("./")
        if clean in ("", "."):
            return "."
        elif f"{clean.rstrip('/')}/" in self.skeleton.children:
            return f"{clean.rstrip('/')}/"
        else:
            return clean

    def owner(self, path: str) -> str:
        if path == "." or path.endswith("/"):
            return self.described_from(path)
        else:
            return self.described_from(parent_of(path))

    def described_from(self, directory: str) -> str:
        if directory in self.described:
            return directory
        else:
            return self.described_from(parent_of(directory))

    def files_of(self, directory: str) -> list[str]:
        found: list[str] = []
        for child in self.skeleton.children.get(directory, []):
            if not child.endswith("/"):
                found.append(child)
        return found

    def digests(self, directory: str) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in self.files_of(directory):
            found[path] = self.skeleton.sources[path].digest
        return found

    def key(self, directory: str, held: Sequence[Memory]) -> str:
        parts = [directory]
        for path in self.files_of(directory):
            source = self.skeleton.sources[path]
            if readme(path):
                parts.append(f"{path} {source.digest}")
            elif source.shape is not None:
                parts.append(f"{path} {interface(source.shape)}")
            else:
                parts.append(path)
        module = self.skeleton.modules.get(directory)
        if module is not None:
            parts.append(f"module {' '.join(module.manifests)} depends {' '.join(module.depends)}")
        for region in self.below[directory]:
            parts.append(f"{region} {text_digest(self.notes.get(region))}")
        for memory in held:
            parts.append(memory.id)
        return hashed("\n".join(parts).encode())

    def keep(self, note: NoteData) -> None:
        write(self.home / note_name(note["path"]), note_page(note))
        self.notes[note["path"]] = note

    def shown(self, directory: str, held: Sequence[Memory]) -> NoteShown | None:
        note = self.notes.get(directory)
        if note is None:
            return None
        else:
            fresh = note["key"] == self.key(directory, held)
            return {"path": directory, "summary": note["summary"], "text": note["text"], "fresh": fresh, "unchecked": note["unchecked"], "time": note["time"]}

    def module_data(self, path: str) -> ModuleData | None:
        module = self.skeleton.module_of(path)
        if module is None:
            return None
        else:
            return {"path": module.path, "manifests": list(module.manifests), "depends": list(module.depends), "used_by": self.skeleton.depended_on_by(module)}

    def parts(self, directory: str) -> list[PartData]:
        found: list[PartData] = []
        for region in self.below.get(directory, []):
            found.append({"path": region, "kind": "region", "summary": summary_of(self.notes.get(region)), "lines": 0, "files": len(self.skeleton.under(region))})
        for child in self.skeleton.children.get(directory, []):
            if child.endswith("/") and len(self.below[child]) == 0 and child not in self.described:
                found.append({"path": child, "kind": "directory", "summary": "", "lines": 0, "files": len(self.skeleton.under(child))})
            elif not child.endswith("/"):
                found.append({"path": child, "kind": "file", "summary": "", "lines": self.skeleton.sources[child].lines, "files": 1})
        return found

    def symbols(self, path: str) -> list[SymbolShown]:
        found: list[SymbolShown] = []
        source = self.skeleton.sources.get(path)
        if source is not None and source.shape is not None:
            for each in source.shape["symbols"]:
                found.append({"name": each["name"], "kind": each["kind"], "line": each["line"], "signature": each["signature"]})
        return found

    def linked(self, path: str) -> tuple[list[CountData], list[CountData]]:
        uses, users = self.skeleton.links()
        if path in self.skeleton.sources:
            return singles(uses.get(path, ())), singles(users.get(path, ()))
        else:
            inside = self.skeleton.under(path)
            return self.grouped(inside, uses, path), self.grouped(inside, users, path)

    def group_of(self, directory: str, other: str) -> str:
        module = self.skeleton.module_of(other)
        if directory in self.skeleton.modules and module is not None:
            return module.path
        else:
            return self.owner(other)

    def grouped(self, inside: Sequence[str], links: dict[str, tuple[str, ...]], directory: str) -> list[CountData]:
        counts: dict[str, int] = {}
        for path in inside:
            for other in links.get(path, ()):
                if not covers(directory, other):
                    region = self.group_of(directory, other)
                    counts[region] = counts.get(region, 0) + 1
        found: list[CountData] = []
        for region, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])):
            found.append({"path": region, "count": count})
        return found

    def file_lines(self, directory: str, full: bool) -> list[str]:
        lines: list[str] = []
        for path in self.files_of(directory):
            source = self.skeleton.sources[path]
            if source.shape is not None and full:
                lines.append(f"- {path} ({source.lines} lines)")
                lines.extend(symbol_lines(source.shape["symbols"], True))
            elif source.shape is not None:
                lines.append(f"- {path} ({source.lines} lines)")
                lines.extend(symbol_lines(outermost(source.shape["symbols"]), False))
        return lines

    def module_line(self, directory: str) -> str:
        module = self.skeleton.module_of(directory)
        if module is not None and module.path == directory:
            depends = ", ".join(module.depends) or message("none")
            users = ", ".join(self.skeleton.depended_on_by(module)) or message("none")
            return message("describe_module", path=module.path, manifests=", ".join(module.manifests), depends=depends, users=users)
        elif module is not None:
            return message("describe_inside", path=module.path, manifests=", ".join(module.manifests))
        else:
            return ""

    def readme_text(self, directory: str) -> str:
        for path in self.files_of(directory):
            if readme(path):
                return cut((self.skeleton.root / path).read_text(errors="replace"), README_ROOM)
        return message("none")

    def below_lines(self, directory: str) -> list[str]:
        lines: list[str] = []
        for region in self.below[directory]:
            note = self.notes.get(region)
            if note is None:
                lines.append(f"- {region}: {message('map_undescribed', files=str(len(self.skeleton.under(region))))}")
            else:
                lines.append(f"- {region}: {note['summary']}\n{note['text']}")
        return lines

    def plain_lines(self, directory: str) -> list[str]:
        lines: list[str] = []
        for child in self.skeleton.children.get(directory, []):
            if child.endswith("/") and child not in self.described and len(self.below[child]) == 0:
                lines.append(f"- {child} ({self.kinds(child)})")
        return lines

    def others(self, directory: str) -> list[str]:
        found: list[str] = []
        for path in self.files_of(directory):
            if self.skeleton.sources[path].shape is None:
                found.append(path)
        return found

    def brief(self, directory: str, held: Sequence[Memory]) -> str:
        uses, users = self.linked(directory)
        leads: list[str] = []
        for memory in held:
            leads.append(lead(memory, ()))
        below = self.below_lines(directory)
        values = {
            "region": region_name(directory),
            "module": self.module_line(directory),
            "readme": self.readme_text(directory),
            "others": listing(self.others(directory), OTHERS),
            "below": listing(below, len(below)),
            "plain": listing(self.plain_lines(directory), OTHERS),
            "uses": listing(counted_lines(uses), LINKED),
            "users": listing(counted_lines(users), LINKED),
            "held": listing(leads, HELD),
        }
        full = message("describe_region", files=listing(self.file_lines(directory, True), len(self.skeleton.sources)), **values)
        if len(full) > ROOM:
            return cut(message("describe_region", files=listing(self.file_lines(directory, False), len(self.skeleton.sources)), **values), ROOM)
        else:
            return full

    def kinds(self, directory: str) -> str:
        counts: dict[str, int] = {}
        total = 0
        for path in self.skeleton.under(directory):
            suffix = PurePosixPath(path).suffix or PurePosixPath(path).name
            counts[suffix] = counts.get(suffix, 0) + 1
            total += 1
        shown: list[str] = []
        for suffix, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:6]:
            shown.append(f"{count} {suffix}")
        return message("map_files", count=str(total), kinds=", ".join(shown))

    def unknown(self, directory: str, page: DescribeArgs) -> list[str]:
        names: set[str] = set()
        for each in names_in(FILENAME.sub(" ", f"{page['summary']}\n{page['text']}")):
            names.add(each.name)
        inside = self.skeleton.under(directory)
        known: set[str] = set()
        for path in inside:
            known.add(PurePosixPath(path).stem)
            known.update(PurePosixPath(path).parts)
        left = names - known
        return sorted(left - mentioned(self.skeleton.root, inside, left))

    def documents(self) -> list[tuple[Located, set[str]]]:
        if self.index is None:
            self.index = []
            for region in self.regions():
                note = self.notes.get(region)
                if note is not None:
                    self.index.append((Located("region", region, note["summary"], "region", 0.0), parts_of(f"{region} {note['summary']} {note['text']}")))
            for path, source in sorted(self.skeleton.sources.items()):
                if source.shape is not None:
                    names: list[str] = []
                    for each in source.shape["symbols"]:
                        names.append(each["name"])
                    self.index.append((Located("file", path, path, "", 0.0), parts_of(f"{path} {' '.join(names)}")))
        return self.index

    def matched(self, each: Located, asked: set[str], score: float) -> Located:
        source = self.skeleton.sources.get(each.path)
        if each.kind == "file" and source is not None and source.shape is not None:
            names: list[str] = []
            for symbol in source.shape["symbols"]:
                if len(parts_of(symbol["name"]) & asked) > 0:
                    names.append(symbol["name"])
            return replace(each, title=", ".join(names[:6]), detail=f"in {self.owner(each.path)}", score=score)
        else:
            return replace(each, score=score)

    def where(self, query: str) -> tuple[list[Located], list[str]]:
        indexed = self.documents()
        asked = parts_of(query)
        documents: list[set[str]] = []
        for _, document in indexed:
            documents.append(document)
        found: list[Located] = []
        for (each, document), score in zip(indexed, scores(" ".join(sorted(asked)), documents)):
            if len(document & asked) > 0:
                found.append(self.matched(each, asked, score))
        found.sort(key=lambda each: -each.score)
        return found, unmatched(query, documents)


def atlas_of(root: Path) -> Atlas:
    found = places(root)
    return Atlas(found, skeleton(found.home, root))


class Writer:
    atlas: Atlas
    extractor: Extractor
    access: Access
    system: str
    parent: str | None

    def __init__(self, atlas: Atlas, extractor: Extractor, access: Access, system: str, parent: str | None) -> None:
        self.atlas = atlas
        self.extractor = extractor
        self.access = access
        self.system = system
        self.parent = parent

    def write(self, pending: Pending) -> NoteData | Failure:
        with span("region", pending.path, str(self.access.root), self.parent, {"files": len(pending.files)}) as doing:
            written = self.written(pending)
            if isinstance(written, Failure):
                doing.ok = False
                doing.attrs["reason"] = written.reason
            else:
                chunk(doing, "note", f"{written['summary']}\n{written['text']}")
            return written

    def written(self, pending: Pending) -> NoteData | Failure:
        settings = self.extractor.settings
        answer = self.extractor.answered(self.system, pending.brief, DESCRIBE, pending.path, settings)
        if isinstance(answer, Failure):
            return answer
        else:
            page = cast(DescribeArgs, answer.answer)
            unknown = self.atlas.unknown(pending.path, page)
            if len(unknown) > 0:
                sent = message("describe_unknown", path=region_name(pending.path), names=", ".join(unknown))
                return self.rewritten(pending, self.extractor.answered_again(answer, sent, DESCRIBE, settings))
            else:
                return noted(pending, page, unknown)

    def rewritten(self, pending: Pending, again: Explored | Failure) -> NoteData | Failure:
        if isinstance(again, Failure):
            return again
        else:
            page = cast(DescribeArgs, again.answer)
            return noted(pending, page, self.atlas.unknown(pending.path, page))


def noted(pending: Pending, page: DescribeArgs, unknown: list[str]) -> NoteData | Failure:
    summary = page["summary"].strip()
    text = page["text"].strip()
    if summary == "" or text == "":
        return Failure("the page came back empty")
    else:
        return {"path": pending.path, "key": pending.key, "summary": summary, "text": text, "unchecked": unknown, "files": pending.files, "time": time.time()}


def describe(found: Places, settings: Settings, usage: Usage, say: Callable[[str], None], within: str, stop: threading.Event) -> Built:
    root = found.repo.root
    atlas = Atlas(found, skeleton(found.home, root))
    store = Store(found.memory)
    watch = staleness(found, store)
    extractor = Extractor(found, replace(settings, effort=settings.light_effort), usage)
    writer = Writer(atlas, extractor, extractor.access(), prompt("describe", root="."), here())
    under = atlas.located(within)
    waves: dict[int, list[str]] = {}
    total = 0
    for region in atlas.regions():
        if covers(under, region):
            waves.setdefault(depth_of(region), []).append(region)
            total += 1
    written = 0
    current = 0
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=settings.parallel) as pool:
        for depth in sorted(waves, reverse=True):
            pending: list[Pending] = []
            for region in waves[depth]:
                held = held_in(store, watch, region)
                key = atlas.key(region, held)
                note = atlas.notes.get(region)
                if note is not None and note["key"] == key:
                    current += 1
                else:
                    pending.append(Pending(region, key, atlas.brief(region, held), atlas.digests(region)))
            for first in range(0, len(pending), settings.parallel):
                batch = pending[first : first + settings.parallel]
                if not stop.is_set():
                    for each, result in zip(batch, pool.map(writer.write, batch)):
                        if isinstance(result, Failure):
                            failed.append(f"{each.path}: {result.reason}")
                            say(message("map_failed", path=each.path, reason=result.reason))
                        else:
                            atlas.keep(result)
                            written += 1
                            say(message("map_written", path=each.path, done=str(written + current + len(failed)), total=str(total), summary=result["summary"]))
    return Built(written, current, tuple(failed))


def region_note(found: Places, path: str) -> NoteData | None:
    best: NoteData | None = None
    for directory, note in notes_in(found).items():
        if directory != "." and covers(directory, path) and (best is None or len(directory) > len(best["path"])):
            best = note
    return best


def changed_since(root: Path, note: NoteData, path: str) -> bool:
    before = note["files"].get(path)
    if before is None:
        return False
    else:
        now = digest(root, path)
        return now is not None and now != before
