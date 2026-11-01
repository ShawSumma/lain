from __future__ import annotations

import sys
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal, Protocol

from bugs import noticed
from page import Builder, Page, Unreadable, named, number, read, real, when, write
from places import USER
from prompts import message
from rank import scores, words
from redact import redacted, redacted_each
from store import locked
from tree import files_under

type MemoryKind = Literal["goal", "option", "decision", "action", "outcome", "observation", "revisit", "retraction"]
type Basis = Literal["user", "verified", "stated", "inferred"]
type EdgeKind = Literal["leads_to", "chosen", "rejected", "supersedes", "restates", "continues"]

TRUST: tuple[Basis, ...] = ("user", "verified", "stated", "inferred")
KINDS: tuple[MemoryKind, ...] = ("goal", "option", "decision", "action", "outcome", "observation", "revisit", "retraction")
EDGES: tuple[EdgeKind, ...] = ("leads_to", "chosen", "rejected", "supersedes", "restates", "continues")
FOLDERS: dict[MemoryKind, str] = {
    "goal": "goals",
    "decision": "decisions",
    "option": "options",
    "action": "actions",
    "outcome": "outcomes",
    "observation": "observations",
    "revisit": "revisits",
    "retraction": "retractions",
}


@dataclass(frozen=True)
class Edge:
    kind: EdgeKind
    origin: str
    target: str
    files: tuple[str, ...]


@dataclass(frozen=True)
class Link:
    kind: EdgeKind
    other: Memory
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class Conditions:
    assumptions: tuple[str, ...]
    revisit_when: tuple[str, ...]


NONE = Conditions((), ())


@dataclass(frozen=True)
class Memory:
    id: str
    kind: MemoryKind
    text: str
    basis: Basis
    source: str
    time: float
    files: tuple[str, ...]
    evidence: tuple[str, ...]
    conditions: Conditions
    edges: tuple[Edge, ...]
    withdrawn: bool

    @property
    def agent(self) -> str:
        return self.source.rpartition("/")[0]

    @property
    def step(self) -> int:
        return int(self.source.rpartition("/")[2])


@dataclass(frozen=True)
class Choice:
    text: str
    taken: bool


def memory_kind(text: str) -> MemoryKind:
    for kind in KINDS:
        if kind == text:
            return kind
    raise Unreadable(f"'{text}' is not a kind of memory")


def basis_of(text: str) -> Basis:
    for basis in TRUST:
        if basis == text:
            return basis
    raise Unreadable(f"'{text}' is not a basis")


def edge_kind(text: str) -> EdgeKind:
    for kind in EDGES:
        if kind == text:
            return kind
    raise Unreadable(f"'{text}' is not a kind of link")


def encode(memory: Memory) -> Page:
    built = Builder(f"{memory.kind} {memory.id}: {memory.text.strip().partition(chr(10))[0]}")
    built.field("id", memory.id)
    built.field("kind", memory.kind)
    built.field("basis", memory.basis)
    built.field("source", memory.source)
    built.field("time", when(memory.time))
    if memory.withdrawn:
        built.field("withdrawn", "yes")
    for path in memory.files:
        built.field("file", path)
    for edge in memory.edges:
        built.field("link", f"{edge.origin} {edge.kind} {edge.target}")
        for path in edge.files:
            built.field("link file", f"{edge.origin} {edge.kind} {edge.target} {path}")
    for assumption in memory.conditions.assumptions:
        built.section("assumes", assumption)
    for condition in memory.conditions.revisit_when:
        built.section("revisit when", condition)
    built.section("text", memory.text)
    for cited in memory.evidence:
        built.section("evidence", cited)
    return built.page()


def decode(page: Page) -> Memory:
    scoped_files: dict[str, list[str]] = {}
    for line in page.every("link file"):
        origin, kind, target, path = line.split(" ", 3)
        scoped_files.setdefault(f"{origin} {kind} {target}", []).append(path)
    edges: list[Edge] = []
    for line in page.every("link"):
        origin, kind, target = line.split(" ")
        edges.append(Edge(edge_kind(kind), origin, target, tuple(scoped_files.get(line, []))))
    return Memory(
        id=page.need("id"),
        kind=memory_kind(page.need("kind")),
        text=page.value("text"),
        basis=basis_of(page.need("basis")),
        source=page.need("source"),
        time=real(page.need("time")),
        files=tuple(page.every("file")),
        evidence=tuple(page.texts("evidence")),
        conditions=Conditions(tuple(page.texts("assumes")), tuple(page.texts("revisit when"))),
        edges=tuple(edges),
        withdrawn=page.one("withdrawn") == "yes",
    )


def scoped(path: str) -> bool:
    if path == "." or path.endswith("/"):
        return True
    else:
        for mark in "*?[":
            if mark in path:
                return True
        return False


def covers(pattern: str, path: str) -> bool:
    if pattern == "." or pattern == path:
        return True
    elif pattern.endswith("/"):
        return path.startswith(pattern)
    elif scoped(pattern):
        return fnmatchcase(path, pattern)
    else:
        return pattern == path


def overlaps(first: Sequence[str], second: Sequence[str]) -> bool:
    for one in first:
        for other in second:
            if covers(one, other) or covers(other, one):
                return True
    return False


def present(root: Path, path: str) -> bool:
    if path == "." or (root / path).exists():
        return True
    elif scoped(path) and not path.endswith("/"):
        for found in files_under(root, "."):
            if fnmatchcase(found, path):
                return True
        return False
    else:
        return False


def redacted_conditions(conditions: Conditions) -> Conditions:
    return Conditions(redacted_each(conditions.assumptions), redacted_each(conditions.revisit_when))


def choice(taken: bool) -> EdgeKind:
    if taken:
        return "chosen"
    else:
        return "rejected"


class Watch(Protocol):
    def fresh(self, memory: Memory) -> bool: ...

    def notes(self, memory: Memory) -> list[str]: ...


def project_of(directory: Path) -> str:
    return str(directory.parent.parent)


def highest(directory: Path) -> int:
    top = 0
    for kind in KINDS:
        folder = directory / FOLDERS[kind]
        if folder.is_dir():
            for path in folder.glob("*.md"):
                head = path.stem.partition("-")[0]
                if head.isdigit():
                    top = max(top, int(head))
    return top


class Store:
    directory: Path
    nodes: dict[str, Memory]
    paths: dict[str, Path]
    keys: dict[tuple[str, str, str], str]
    incoming: dict[str, list[Edge]]
    top: int

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.nodes = {}
        self.paths = {}
        self.keys = {}
        self.incoming = {}
        self.top = 0
        for kind in KINDS:
            folder = directory / FOLDERS[kind]
            if folder.is_dir():
                for path in sorted(folder.glob("*.md")):
                    self.load(path)

    def load(self, path: Path) -> None:
        try:
            memory = decode(read(path))
        except (Unreadable, ValueError, OSError) as error:
            noticed("memory", project_of(self.directory), f"the memory page {path.name} cannot be read, so it is left out: {error}", str(path))
        else:
            self.top = max(self.top, number(memory.id))
            self.paths[memory.id] = path
            self.keys[(memory.kind, memory.text, memory.source)] = memory.id
            if not memory.withdrawn:
                self.keep(memory)

    def keep(self, memory: Memory) -> None:
        self.nodes[memory.id] = memory
        for edge in memory.edges:
            self.incoming.setdefault(edge.target, []).append(edge)

    def find(self, kind: MemoryKind, text: str, source: str) -> str | None:
        return self.keys.get((kind, redacted(text), source))

    def save(self, memory: Memory) -> None:
        path = self.paths.get(memory.id)
        if path is None:
            path = self.directory / FOLDERS[memory.kind] / named(number(memory.id), memory.text.strip().partition("\n")[0])
            self.paths[memory.id] = path
        write(path, encode(memory))

    def add(
        self,
        kind: MemoryKind,
        text: str,
        basis: Basis,
        source: str,
        stamp: float,
        files: Sequence[str],
        incoming: Sequence[Link],
        outgoing: Sequence[Link],
        evidence: Sequence[str],
        conditions: Conditions,
    ) -> Memory:
        text = redacted(text)
        known = self.keys.get((kind, text, source))
        if known is not None and known in self.nodes:
            return self.nodes[known]
        else:
            with locked(self.directory):
                ident = known
                if ident is None:
                    self.top = max(self.top, highest(self.directory)) + 1
                    ident = str(self.top)
                edges: list[Edge] = []
                for link in incoming:
                    edges.append(Edge(link.kind, link.other.id, ident, link.files))
                for link in outgoing:
                    edges.append(Edge(link.kind, ident, link.other.id, link.files))
                memory = Memory(
                    ident, kind, text, basis, source, stamp, tuple(sorted(files)), redacted_each(evidence), redacted_conditions(conditions), tuple(edges), False
                )
                self.save(memory)
                self.keys[(kind, text, source)] = ident
                self.keep(memory)
                return memory

    def withdraw(self, agent: str, live: set[int]) -> list[Memory]:
        gone: list[Memory] = []
        for memory in list(self.nodes.values()):
            if memory.agent == agent and memory.step not in live:
                del self.nodes[memory.id]
                gone.append(memory)
        for target, edges in self.incoming.items():
            kept: list[Edge] = []
            for edge in edges:
                if edge.origin in self.nodes:
                    kept.append(edge)
            self.incoming[target] = kept
        for memory in gone:
            self.save(replace(memory, withdrawn=True))
        return gone

    def decide(
        self,
        goal: Memory,
        chose: str,
        choices: Sequence[Choice],
        files: Sequence[str],
        basis: Basis,
        source: str,
        stamp: float,
        replaced: Sequence[Link],
        evidence: Sequence[str],
        conditions: Conditions,
    ) -> Memory:
        decision = self.add("decision", chose, basis, source, stamp, files, (Link("leads_to", goal),), replaced, evidence, conditions)
        for each in choices:
            self.add("option", each.text, "inferred", source, stamp, files, (Link(choice(each.taken), decision),), (), (), NONE)
        return decision

    def revisit(self, old: Memory, decision: Memory, lesson: Memory | None, source: str, stamp: float) -> Memory:
        because: list[Link] = []
        if lesson is not None:
            because.append(Link("leads_to", lesson))
        return self.add("revisit", old.text, "inferred", source, stamp, old.files, because, (Link("leads_to", decision),), (), NONE)

    def correct(
        self,
        old: Memory,
        text: str,
        files: Sequence[str],
        conditions: Conditions,
        basis: Basis,
        source: str,
        stamp: float,
        evidence: Sequence[str],
    ) -> Memory:
        under: list[Link] = []
        for edge in old.edges:
            if edge.kind == "leads_to" and edge.target == old.id and edge.origin in self.nodes:
                under.append(Link("leads_to", self.nodes[edge.origin]))
        kept: list[Link] = [Link("supersedes", old)]
        for edge in self.edges():
            if edge.origin == old.id and edge.kind in ("chosen", "rejected") and edge.target in self.nodes:
                kept.append(Link(edge.kind, self.nodes[edge.target]))
        return self.add(old.kind, text, basis, source, stamp, files, under, kept, evidence, conditions)

    def retract(self, old: Memory, text: str, basis: Basis, source: str, stamp: float, evidence: Sequence[str]) -> Memory:
        return self.add("retraction", text, basis, source, stamp, old.files, (), (Link("supersedes", old),), evidence, NONE)

    def edges(self) -> list[Edge]:
        found: list[Edge] = []
        for ident in sorted(self.nodes, key=int):
            found.extend(self.nodes[ident].edges)
        return found

    def replacements(self, memory: Memory) -> list[Edge]:
        found: list[Edge] = []
        for edge in self.incoming.get(memory.id, []):
            if edge.kind == "supersedes":
                found.append(edge)
        return found

    def current_files(self, memory: Memory) -> tuple[str, ...]:
        gone: set[str] = set()
        for edge in self.replacements(memory):
            if len(edge.files) == 0:
                return ()
            else:
                gone.update(edge.files)
        kept: list[str] = []
        for path in memory.files:
            if path not in gone:
                kept.append(path)
        return tuple(kept)

    def superseded(self, memory: Memory) -> bool:
        return len(self.replacements(memory)) > 0 and len(self.current_files(memory)) == 0

    def pointed(self, memory: Memory, kind: EdgeKind) -> bool:
        for edge in self.incoming.get(memory.id, []):
            if edge.kind == kind:
                return True
        return False

    def open_decisions(self, files: Sequence[str]) -> list[Memory]:
        found: list[Memory] = []
        for memory in self.ordered():
            if memory.kind == "decision" and not self.superseded(memory) and overlaps(self.current_files(memory), files):
                found.append(memory)
        return found

    def replacing(self, changed: Sequence[str], named_ids: Sequence[str], basis: Basis) -> list[Link]:
        found: list[Link] = []
        for old in self.open_decisions(changed):
            if old.id in named_ids and (old.agent != USER or basis == "user"):
                files: list[str] = []
                for path in self.current_files(old):
                    if overlaps((path,), changed):
                        files.append(path)
                found.append(Link("supersedes", old, tuple(files)))
        return found

    def wanted(self, memory: Memory, files: Sequence[str] | None) -> bool:
        current = self.current_files(memory)
        holds = not self.superseded(memory) and (files is None or overlaps(current, files))
        match memory.kind:
            case "decision" | "observation":
                return holds
            case "option":
                return holds and self.pointed(memory, "rejected")
            case _:
                return False

    def ordered(self) -> list[Memory]:
        return sorted(self.nodes.values(), key=lambda memory: (memory.time, int(memory.id)))

    def relevant(self, files: Sequence[str] | None, watch: Watch) -> list[Memory]:
        found: list[Memory] = []
        for memory in self.ordered():
            if self.wanted(memory, files) and watch.fresh(memory):
                found.append(memory)
        return found

    def ranked(self, found: Sequence[Memory], query: str) -> list[Memory]:
        documents: list[set[str]] = []
        for memory in found:
            documents.append(words(f"{memory.text} {' '.join(memory.files)}"))
        ranks = scores(query, documents)
        kept: list[Memory] = []
        for index in sorted(range(len(found)), key=lambda index: (-ranks[index], broad(found[index]), standing(found[index]))):
            kept.append(found[index])
        return kept

    def leads(self, files: Sequence[str] | None, query: str, watch: Watch, limit: int) -> str:
        found = self.ranked(self.relevant(files, watch), query)
        lines: list[str] = []
        for memory in found[:limit]:
            lines.append(lead(memory, watch.notes(memory)))
        if len(found) > limit:
            lines.append(message("leads_left_out", count=str(len(found) - limit)))
        if len(lines) == 0:
            return message("no_leads")
        else:
            return "\n".join([message("lead_bases"), *lines])

    def neighbours(self, ident: str) -> list[Memory]:
        found: list[Memory] = []
        for edge in self.edges():
            if edge.origin == ident and edge.target in self.nodes:
                found.append(self.nodes[edge.target])
            elif edge.target == ident and edge.origin in self.nodes:
                found.append(self.nodes[edge.origin])
        return found

    def chain(self, ident: str) -> list[Memory]:
        found: list[str] = [ident]
        index = 0
        while index < len(found):
            for other in self.neighbours(found[index]):
                if other.id not in found:
                    found.append(other.id)
            index += 1
        kept: list[Memory] = []
        for memory in self.ordered():
            if memory.id in found:
                kept.append(memory)
        return kept


def broad(memory: Memory) -> bool:
    for path in memory.files:
        if not scoped(path):
            return False
    return True


def standing(memory: Memory) -> tuple[int, int]:
    trust = TRUST.index(memory.basis)
    if memory.basis == "user":
        return 0, trust
    elif memory.kind == "decision":
        return 1, trust
    elif memory.kind == "option":
        return 2, trust
    else:
        return 3, trust


def survey(directory: Path) -> list[str]:
    store = Store(directory)
    counts: Counter[str] = Counter()
    for memory in store.nodes.values():
        counts[f"{memory.kind} {memory.basis}"] += 1
        for edge in memory.edges:
            counts[f"link {edge.kind}"] += 1
    lines: list[str] = []
    for shape, count in counts.most_common():
        lines.append(f"{count:8} {shape}")
    lines.append(f"lain reads {len(store.nodes)} memories; pages it cannot read are recorded as bugs")
    return lines


def lead(memory: Memory, notes: Sequence[str]) -> str:
    lines = [message("lead", kind=memory.kind, id=memory.id, basis=memory.basis, text=memory.text, source=memory.source)]
    if len(notes) > 0:
        lines.append(message("lead_stale", items="; ".join(notes)))
    if len(memory.conditions.assumptions) > 0:
        lines.append(message("lead_assumes", items="; ".join(memory.conditions.assumptions)))
    if len(memory.conditions.revisit_when) > 0:
        lines.append(message("lead_revisit", items="; ".join(memory.conditions.revisit_when)))
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    for raw in argv:
        for line in survey(Path(raw)):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
