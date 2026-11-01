from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict

from agent import paths_changed
from commits import COMMITS, Name, Past, lains, reached
from atlas import Atlas, CountData, ModuleData, NoteShown, PartData, SymbolShown, atlas_of, held_in, holding, region_name
from extract import Stretch, stretches
from history import History, Result, Step
from ingest import agent_names
from memory import Edge, EdgeKind, Memory, Store, overlaps, present, scoped
from page import Page, pages, read, real
from places import GIT, SESSIONS, Places
from rank import scores, unmatched, words
from stale import Staleness, staleness

TOGETHER = 10
REGION_MEMORY = 12
WHERE_HITS = 20
SEARCH_HITS = 30
STORIES = 50
TITLE = 200
PARTS = 150


class ItemData(TypedDict):
    id: str
    kind: str
    basis: str
    text: str
    source: str
    time: float
    files: list[str]
    evidence: list[str]
    assumptions: list[str]
    revisit_when: list[str]


class ReplacedData(TypedDict):
    by: str
    files: list[str]


class DecisionData(TypedDict):
    decision: ItemData
    taken: list[ItemData]
    rejected: list[ItemData]
    replaced: list[ReplacedData]
    open: list[str]


class EntryData(TypedDict):
    ref: str
    agent: str
    title: str
    time: float
    session: bool
    changed: int
    remembered: int


class RelatedData(TypedDict):
    ref: str
    title: str


class StoryData(TypedDict):
    ref: str
    agent: str
    time: float
    request: str
    rules: list[ItemData]
    decisions: list[DecisionData]
    done: list[ItemData]
    learned: list[ItemData]
    unverified: list[ItemData]
    changed: list[str]
    related: list[RelatedData]


class TouchData(TypedDict):
    ref: str
    title: str
    time: float
    steps: list[int]


class ChainData(TypedDict):
    old: ItemData
    new: ItemData
    files: list[str]
    because: list[ItemData]


class ListedData(TypedDict):
    path: str
    gone: bool


class TogetherData(TypedDict):
    path: str
    count: int


class PastData(TypedDict):
    names: list[str]
    commits: int
    first: float | None
    last: float | None
    together: list[TogetherData]


class FileData(TypedDict):
    path: str
    gone: bool
    past: PastData
    rules: list[ItemData]
    open: list[DecisionData]
    superseded: list[DecisionData]
    history: list[ChainData]
    rejected: list[ItemData]
    learned: list[ItemData]
    unverified: list[ItemData]
    changes: list[TouchData]
    reads: list[TouchData]


class CompetingData(TypedDict):
    path: str
    decisions: list[ItemData]


class StaleData(TypedDict):
    item: ItemData
    reasons: list[str]


class ConsolidationData(TypedDict):
    competing: list[CompetingData]
    duplicates: list[list[ItemData]]
    restated: list[list[ItemData]]
    gone: list[ItemData]
    conditional: list[ItemData]
    stale: list[StaleData]


class RegionData(TypedDict):
    path: str
    kind: RegionKind
    gone: bool
    regions: int
    written: int
    note: NoteShown | None
    module: ModuleData | None
    parts: list[PartData]
    symbols: list[SymbolShown]
    uses: list[CountData]
    used_by: list[CountData]
    together: list[TogetherData]
    rules: list[ItemData]
    open: list[DecisionData]
    learned: list[ItemData]
    unverified: list[ItemData]
    left: int


type HitKind = Literal["story", "memory", "file", "region"]
type RegionKind = Literal["project", "directory", "file"]


class HitData(TypedDict):
    kind: HitKind
    ref: str
    title: str
    detail: str
    score: float
    superseded: bool


class SearchData(TypedDict):
    hits: list[HitData]
    unmatched: list[str]
    more: int


@dataclass(frozen=True)
class Document:
    hit: HitData
    words: set[str]


def first_line(text: str) -> str:
    return text.strip().partition("\n")[0]


def read_in(step: Step) -> list[str]:
    found: list[str] = []
    match step.body:
        case Result(reads=reads) if reads is not None:
            for seen in reads:
                if seen.kind != "bound":
                    found.append(seen.path)
        case _:
            pass
    return found


def changed_paths(stretch: Stretch) -> list[str]:
    found: list[str] = []
    for step in stretch.steps:
        for path in paths_changed(step):
            if path not in found:
                found.append(path)
    return sorted(found)


def region_kind(path: str) -> RegionKind:
    if path == ".":
        return "project"
    elif path.endswith("/"):
        return "directory"
    else:
        return "file"


def item_of(memory: Memory) -> ItemData:
    return {
        "id": memory.id,
        "kind": memory.kind,
        "basis": memory.basis,
        "text": memory.text,
        "source": memory.source,
        "time": memory.time,
        "files": list(memory.files),
        "evidence": list(memory.evidence),
        "assumptions": list(memory.conditions.assumptions),
        "revisit_when": list(memory.conditions.revisit_when),
    }


class Library:
    root: Path
    commits: Path
    store: Store
    past: Past
    staleness: Staleness
    found: list[Stretch]
    outgoing: dict[str, list[Edge]]
    atlas: Atlas | None

    def __init__(self, at: Places, store: Store) -> None:
        self.root = at.repo.root
        self.commits = at.agent(GIT) / COMMITS
        self.store = store
        self.atlas = None
        self.past = Past(self.root)
        self.staleness = staleness(at, store)
        self.found = []
        for name in agent_names(at):
            self.found.extend(stretches(name, History(at.agent(name)), set(), True))
        self.found.sort(key=lambda stretch: stretch.request.time, reverse=True)
        self.outgoing = {}
        for edge in store.edges():
            self.outgoing.setdefault(edge.origin, []).append(edge)

    def item(self, memory: Memory) -> ItemData:
        return item_of(memory)

    def stretch_holding(self, ident: str) -> str | None:
        memory = self.store.nodes.get(ident)
        if memory is not None:
            for stretch in self.found:
                if stretch.agent == memory.agent and memory.step in stretch.numbers():
                    return stretch.ref(stretch.request.number)
        return None

    def stretch(self, ref: str) -> Stretch | None:
        agent, _, number = ref.rpartition("/")
        if number.isdigit():
            for stretch in self.found:
                if stretch.agent == agent and int(number) in stretch.numbers():
                    return stretch
        return None

    def nodes_in(self, stretch: Stretch) -> list[Memory]:
        numbers = stretch.numbers()
        found: list[Memory] = []
        for memory in self.store.ordered():
            if memory.agent == stretch.agent and memory.step in numbers:
                found.append(memory)
        return found

    def commit_pages(self) -> list[tuple[str, Page]]:
        found: list[tuple[str, Page]] = []
        for path in pages(self.commits):
            found.append((f"{GIT}/{path.stem}", read(path)))
        return found

    def commit_story(self, ref: str) -> StoryData | None:
        path = self.commits / f"{ref.removeprefix(f'{GIT}/')}.md"
        if not ref.startswith(f"{GIT}/") or not path.is_file():
            return None
        else:
            page = read(path)
            found: StoryData = {
                "ref": ref,
                "agent": ref,
                "time": real(page.need("time")),
                "request": page.value("message"),
                "rules": [],
                "decisions": [],
                "done": [],
                "learned": [],
                "unverified": [],
                "changed": page.every("change"),
                "related": [],
            }
            for memory in self.store.ordered():
                if memory.agent == ref and memory.kind == "decision":
                    found["decisions"].append(self.decision(memory))
                elif memory.agent == ref and memory.kind in ("observation", "outcome", "action"):
                    sort_claim(self.item(memory), found["rules"], found["done"], found["learned"], found["unverified"])
            return found

    def entries(self) -> list[EntryData]:
        found: list[EntryData] = []
        for ref, page in self.commit_pages():
            found.append({
                "ref": ref,
                "agent": GIT,
                "title": first_line(page.value("message")),
                "time": real(page.need("time")),
                "session": False,
                "changed": len(page.every("change")),
                "remembered": len(page.every("memory")),
            })
        for stretch in self.found:
            found.append({
                "ref": stretch.ref(stretch.request.number),
                "agent": stretch.agent,
                "title": first_line(stretch.asked),
                "time": stretch.request.time,
                "session": stretch.agent.startswith(SESSIONS),
                "changed": len(changed_paths(stretch)),
                "remembered": len(self.nodes_in(stretch)),
            })
        found.sort(key=lambda entry: entry["time"], reverse=True)
        return found

    def decision(self, memory: Memory) -> DecisionData:
        taken: list[ItemData] = []
        rejected: list[ItemData] = []
        for edge in self.outgoing.get(memory.id, []):
            if edge.target in self.store.nodes and edge.kind == "chosen":
                taken.append(self.item(self.store.nodes[edge.target]))
            elif edge.target in self.store.nodes and edge.kind == "rejected":
                rejected.append(self.item(self.store.nodes[edge.target]))
        replaced: list[ReplacedData] = []
        for edge in self.store.replacements(memory):
            replaced.append({"by": edge.origin, "files": list(edge.files)})
        return {
            "decision": self.item(memory),
            "taken": taken,
            "rejected": rejected,
            "replaced": replaced,
            "open": list(self.store.current_files(memory)),
        }

    def story(self, stretch: Stretch) -> StoryData:
        found: StoryData = {
            "ref": stretch.ref(stretch.request.number),
            "agent": stretch.agent,
            "time": stretch.request.time,
            "request": stretch.asked,
            "rules": [],
            "decisions": [],
            "done": [],
            "learned": [],
            "unverified": [],
            "changed": changed_paths(stretch),
            "related": [],
        }
        goals: list[str] = []
        for memory in self.nodes_in(stretch):
            if memory.kind == "decision":
                found["decisions"].append(self.decision(memory))
            elif memory.kind in ("observation", "outcome", "action"):
                sort_claim(self.item(memory), found["rules"], found["done"], found["learned"], found["unverified"])
            elif memory.kind == "goal":
                goals.append(memory.id)
        seen = {found["ref"]}
        for ident in component(self.linked("continues"), goals):
            ref = self.stretch_holding(ident)
            if ref is not None and ref not in seen:
                other = self.stretch(ref)
                if other is not None:
                    seen.add(ref)
                    found["related"].append({"ref": ref, "title": first_line(other.asked)})
        return found

    def linked(self, kind: EdgeKind) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for edge in self.store.edges():
            if edge.kind == kind and edge.target in self.store.nodes:
                found.setdefault(edge.origin, set()).add(edge.target)
                found.setdefault(edge.target, set()).add(edge.origin)
        return found

    def recorded(self) -> set[str]:
        found: set[str] = set()
        for memory in self.store.nodes.values():
            for path in memory.files:
                if not scoped(path):
                    found.add(path)
        for stretch in self.found:
            for path in changed_paths(stretch):
                if not lains(path):
                    found.add(path)
        return found

    def files(self) -> list[str]:
        found = self.recorded()
        for commit in self.past.commits:
            for path in commit.files:
                if path not in found and (self.root / path).is_file():
                    found.add(path)
        return sorted(found)

    def listing(self) -> list[ListedData]:
        found: list[ListedData] = []
        for path in self.files():
            found.append({"path": path, "gone": not (self.root / path).exists()})
        return found

    def touches(self, path: str, reading: bool) -> list[TouchData]:
        found: list[TouchData] = []
        for stretch in self.found:
            steps: list[int] = []
            for step in stretch.steps:
                if reading and path in read_in(step):
                    steps.append(step.number)
                elif not reading and path in paths_changed(step):
                    steps.append(step.number)
            if len(steps) > 0:
                title = first_line(stretch.asked)
                found.append({"ref": stretch.ref(stretch.request.number), "title": title, "time": stretch.request.time, "steps": steps})
        return found

    def remembered(self, names: Sequence[Name]) -> list[Memory]:
        found: list[Memory] = []
        for memory in self.store.ordered():
            if reached(memory.files, memory.time, names):
                found.append(memory)
        return found

    def past_of(self, path: str, names: Sequence[Name]) -> PastData:
        earlier: list[str] = []
        for name in names[1:]:
            earlier.append(name.path)
        commits = self.past.changes(path)
        together: list[TogetherData] = []
        for other, count in self.past.together(path, self.root):
            together.append({"path": other, "count": count})
        found: PastData = {"names": earlier, "commits": len(commits), "first": None, "last": None, "together": together}
        if len(commits) > 0:
            found["first"] = commits[0].time
            found["last"] = commits[-1].time
        return found

    def file(self, path: str) -> FileData:
        names = self.past.names(path)
        own: list[str] = []
        for name in names:
            own.append(name.path)
        found: FileData = {
            "path": path,
            "gone": not (self.root / path).exists(),
            "past": self.past_of(path, names),
            "rules": [],
            "open": [],
            "superseded": [],
            "history": self.history(names),
            "rejected": [],
            "learned": [],
            "unverified": [],
            "changes": self.touches(path, False),
            "reads": self.touches(path, True),
        }
        done: list[ItemData] = []
        for memory in self.remembered(names):
            if memory.kind == "decision" and overlaps(self.store.current_files(memory), own):
                found["open"].append(self.decision(memory))
            elif memory.kind == "decision":
                found["superseded"].append(self.decision(memory))
            elif memory.kind == "option" and self.store.pointed(memory, "rejected"):
                found["rejected"].append(self.item(memory))
            elif memory.kind in ("observation", "outcome", "action"):
                sort_claim(self.item(memory), found["rules"], done, found["learned"], found["unverified"])
        found["learned"].extend(done)
        return found

    def mapped(self) -> Atlas:
        if self.atlas is None:
            self.atlas = atlas_of(self.root)
        return self.atlas

    def region(self, path: str) -> RegionData:
        atlas = self.mapped()
        at = atlas.located(path)
        kind = region_kind(at)
        owner = atlas.owner(at)
        uses, used_by = atlas.linked(at)
        page: RegionData = {
            "path": at,
            "kind": kind,
            "gone": atlas.gone(at),
            "regions": len(atlas.described),
            "written": atlas.written(),
            "note": atlas.shown(owner, held_in(self.store, self.staleness, owner)),
            "module": atlas.module_data(at),
            "parts": atlas.parts(at),
            "symbols": atlas.symbols(at),
            "uses": uses,
            "used_by": used_by,
            "together": [],
            "rules": [],
            "open": [],
            "learned": [],
            "unverified": [],
            "left": 0,
        }
        if kind == "file":
            for other, count in self.past.together(at, self.root)[:TOGETHER]:
                page["together"].append({"path": other, "count": count})
        held = holding(self.store, self.staleness, at)
        done: list[ItemData] = []
        for memory in held[:REGION_MEMORY]:
            if memory.kind == "decision":
                page["open"].append(self.decision(memory))
            else:
                sort_claim(self.item(memory), page["rules"], done, page["learned"], page["unverified"])
        page["learned"].extend(done)
        page["left"] = max(0, len(held) - REGION_MEMORY)
        return page

    def where(self, query: str) -> SearchData:
        found, missing = self.mapped().where(query)
        hits: list[HitData] = []
        for each in found[:WHERE_HITS]:
            hits.append({"kind": each.kind, "ref": each.path, "title": each.title, "detail": each.detail, "score": each.score, "superseded": False})
        return {"hits": hits, "unmatched": missing, "more": max(0, len(found) - WHERE_HITS)}

    def lessons(self, decision: Memory) -> list[ItemData]:
        found: list[ItemData] = []
        for edge in self.store.incoming.get(decision.id, []):
            revisit = self.store.nodes.get(edge.origin)
            if revisit is not None and revisit.kind == "revisit":
                for reason in self.store.incoming.get(revisit.id, []):
                    lesson = self.store.nodes.get(reason.origin)
                    if lesson is not None and lesson.kind == "observation":
                        found.append(self.item(lesson))
        return found

    def history(self, names: Sequence[Name]) -> list[ChainData]:
        own: list[str] = []
        for name in names:
            own.append(name.path)
        found: list[ChainData] = []
        for memory in self.remembered(names):
            if memory.kind == "decision":
                for edge in self.store.replacements(memory):
                    newer = self.store.nodes.get(edge.origin)
                    if newer is not None and (len(edge.files) == 0 or overlaps(edge.files, own)):
                        found.append(self.chain(memory, edge, newer))
        return found

    def chain(self, memory: Memory, edge: Edge, newer: Memory) -> ChainData:
        return {"old": self.item(memory), "new": self.item(newer), "files": list(edge.files), "because": self.lessons(newer)}

    def open_decisions(self) -> list[Memory]:
        found: list[Memory] = []
        for memory in self.store.ordered():
            if memory.kind == "decision" and not self.store.superseded(memory):
                found.append(memory)
        return found

    def pivots(self) -> list[ChainData]:
        found: list[ChainData] = []
        for memory in self.store.ordered():
            if memory.kind == "decision":
                for edge in self.store.replacements(memory):
                    newer = self.store.nodes.get(edge.origin)
                    if newer is not None:
                        found.append(self.chain(memory, edge, newer))
        found.sort(key=lambda chain: chain["new"]["time"], reverse=True)
        return found

    def consolidation(self) -> ConsolidationData:
        found: ConsolidationData = {
            "competing": [],
            "duplicates": [],
            "restated": self.restated(),
            "gone": [],
            "conditional": [],
            "stale": [],
        }
        for flagged in self.staleness.flagged():
            found["stale"].append({"item": self.item(flagged.memory), "reasons": list(flagged.reasons)})
        decisions = self.open_decisions()
        for path in sorted(self.recorded()):
            held: list[ItemData] = []
            for memory in decisions:
                if path in self.store.current_files(memory):
                    held.append(self.item(memory))
            if len(held) > 1:
                found["competing"].append({"path": path, "decisions": held})
        same: dict[tuple[str, str], list[ItemData]] = {}
        for memory in self.store.ordered():
            if memory.kind in ("decision", "observation") and not self.store.superseded(memory):
                same.setdefault((memory.kind, memory.text.strip().casefold()), []).append(self.item(memory))
        for group in same.values():
            if len(group) > 1:
                found["duplicates"].append(group)
        for memory in decisions:
            current = self.store.current_files(memory)
            if len(current) > 0 and not self.exists(current):
                found["gone"].append(self.item(memory))
            if len(memory.conditions.revisit_when) > 0:
                found["conditional"].append(self.item(memory))
        return found

    def restated(self) -> list[list[ItemData]]:
        linked = self.linked("restates")
        seen: set[str] = set()
        groups: list[list[ItemData]] = []
        for memory in self.store.ordered():
            if memory.id in linked and memory.id not in seen:
                group = component(linked, [memory.id])
                seen.update(group)
                kept: list[ItemData] = []
                for each in self.store.ordered():
                    if each.id in group:
                        kept.append(self.item(each))
                groups.append(kept)
        return groups

    def exists(self, paths: Sequence[str]) -> bool:
        for path in paths:
            if present(self.root, path):
                return True
        return False

    def documents(self) -> list[Document]:
        found: list[Document] = []
        for ref, page in self.commit_pages():
            text = page.value("message")
            hit: HitData = {"kind": "story", "ref": ref, "title": first_line(text), "detail": "commit", "score": 0.0, "superseded": False}
            found.append(Document(hit, words(f"{text} {' '.join(page.every('change'))}")))
        for stretch in self.found:
            text = stretch.asked
            ref = stretch.ref(stretch.request.number)
            hit: HitData = {"kind": "story", "ref": ref, "title": first_line(text), "detail": stretch.agent, "score": 0.0, "superseded": False}
            found.append(Document(hit, words(f"{text} {' '.join(changed_paths(stretch))}")))
        for memory in self.store.ordered():
            if memory.kind != "goal":
                detail = f"{memory.kind}, {memory.basis}, from {memory.source}{self.replaced_note(memory)}"
                superseded = len(self.store.replacements(memory)) > 0
                hit = {"kind": "memory", "ref": memory.id, "title": first_line(memory.text), "detail": detail, "score": 0.0, "superseded": superseded}
                found.append(Document(hit, words(f"{memory.text} {' '.join(memory.files)}")))
        for path in sorted(self.recorded()):
            hit = {"kind": "file", "ref": path, "title": path, "detail": "file", "score": 0.0, "superseded": False}
            found.append(Document(hit, words(path)))
        return found

    def replaced_note(self, memory: Memory) -> str:
        if self.store.superseded(memory):
            return ", superseded"
        elif len(self.store.replacements(memory)) > 0:
            gone: list[str] = []
            for path in memory.files:
                if path not in self.store.current_files(memory):
                    gone.append(path)
            return f", superseded for {', '.join(gone)}"
        else:
            return ""

    def search(self, query: str) -> SearchData:
        documents = self.documents()
        sets: list[set[str]] = []
        for document in documents:
            sets.append(document.words)
        hits: list[HitData] = []
        for document, score in zip(documents, scores(query, sets)):
            if score > 0:
                hit: HitData = {
                    "kind": document.hit["kind"],
                    "ref": document.hit["ref"],
                    "title": document.hit["title"],
                    "detail": document.hit["detail"],
                    "score": score,
                    "superseded": document.hit["superseded"],
                }
                hits.append(hit)
        hits.sort(key=lambda hit: hit["score"], reverse=True)
        return {"hits": self.replacements_first(hits[:SEARCH_HITS]), "unmatched": unmatched(query, sets), "more": max(0, len(hits) - SEARCH_HITS)}

    def replacements_first(self, hits: Sequence[HitData]) -> list[HitData]:
        found: dict[str, HitData] = {}
        for hit in hits:
            if hit["kind"] == "memory":
                found[hit["ref"]] = hit
        placed: list[HitData] = []
        done: set[str] = set()
        for hit in hits:
            self.place(hit, found, placed, done)
        return placed

    def place(self, hit: HitData, found: Mapping[str, HitData], placed: list[HitData], done: set[str]) -> None:
        key = f"{hit['kind']} {hit['ref']}"
        if key not in done:
            done.add(key)
            memory = self.store.nodes.get(hit["ref"])
            if hit["kind"] == "memory" and memory is not None:
                for edge in self.store.replacements(memory):
                    newer = found.get(edge.origin)
                    if newer is not None:
                        self.place(newer, found, placed, done)
            placed.append(hit)


def component(linked: Mapping[str, set[str]], start: Sequence[str]) -> list[str]:
    group = list(start)
    index = 0
    while index < len(group):
        for other in sorted(linked.get(group[index], set())):
            if other not in group:
                group.append(other)
        index += 1
    return group


def sort_claim(entry: ItemData, rules: list[ItemData], done: list[ItemData], learned: list[ItemData], unverified: list[ItemData]) -> None:
    if entry["basis"] == "user":
        rules.append(entry)
    elif entry["basis"] in ("stated", "inferred"):
        unverified.append(entry)
    elif entry["kind"] == "observation":
        learned.append(entry)
    else:
        done.append(entry)


def cited(entry: ItemData) -> str:
    files = ""
    if len(entry["files"]) > 0:
        files = f" ({', '.join(entry['files'])})"
    stated = ""
    if entry["basis"] == "stated":
        stated = ", stated"
    lines = [f"- {entry['text']}{files} [from {entry['source']}, memory {entry['id']}{stated}]"]
    for assumption in entry["assumptions"]:
        lines.append(f"  - assumes: {assumption}")
    for condition in entry["revisit_when"]:
        lines.append(f"  - revisit when: {condition}")
    return "\n".join(lines)


def section(title: str, entries: Sequence[ItemData]) -> list[str]:
    lines: list[str] = []
    if len(entries) > 0:
        lines.extend(["", f"## {title}", ""])
        for entry in entries:
            lines.append(cited(entry))
    return lines


def decision_lines(decisions: Sequence[DecisionData], title: str) -> list[str]:
    lines: list[str] = []
    if len(decisions) > 0:
        lines.extend(["", f"## {title}", ""])
        for each in decisions:
            lines.append(cited(each["decision"]))
            for option in each["rejected"]:
                lines.append(f"  - turned down: {option['text']}")
            for replaced in each["replaced"]:
                lines.append(f"  - replaced by {replaced['by']} {scope_of(replaced['files'])}")
    return lines


def scope_of(files: Sequence[str]) -> str:
    if len(files) == 0:
        return "everywhere"
    else:
        return f"for {', '.join(files)}"


def quoted(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.strip().splitlines():
        lines.append(f"> {line}")
    return lines


def story_markdown(story: StoryData) -> str:
    lines: list[str] = [f"# {first_line(story['request'])}", "", f"{story['ref']}", ""]
    lines.extend(quoted(story["request"]))
    lines.extend(section("Rules given", story["rules"]))
    lines.extend(decision_lines(story["decisions"], "Decisions"))
    lines.extend(section("Done", story["done"]))
    lines.extend(section("Learned", story["learned"]))
    if len(story["changed"]) > 0:
        lines.extend(["", "## Changed", ""])
        for path in story["changed"]:
            lines.append(f"- {path}")
    lines.extend(section("Not verified", story["unverified"]))
    if len(story["related"]) > 0:
        lines.extend(["", "## Part of the same work", ""])
        for related in story["related"]:
            lines.append(f"- {related['title']} [{related['ref']}]")
    return "\n".join(lines) + "\n"


def touch_lines(title: str, touches: Sequence[TouchData]) -> list[str]:
    lines: list[str] = []
    if len(touches) > 0:
        lines.extend(["", f"## {title}", ""])
        for touch in touches:
            steps: list[str] = []
            for number in touch["steps"]:
                steps.append(str(number))
            lines.append(f"- {touch['title']} [{touch['ref']}, steps {', '.join(steps)}]")
    return lines


def chain_lines(chains: Sequence[ChainData]) -> list[str]:
    lines: list[str] = []
    if len(chains) > 0:
        lines.extend(["", "## How it got here", ""])
        for chain in chains:
            lines.append(f"- {chain['old']['text']} [{chain['old']['id']}]")
            lines.append(f"  - replaced {scope_of(chain['files'])} by: {chain['new']['text']} [{chain['new']['id']}, from {chain['new']['source']}]")
            for lesson in chain["because"]:
                lines.append(f"  - because: {lesson['text']}")
    return lines


def consolidation_markdown(found: ConsolidationData) -> str:
    lines: list[str] = ["# What memory disagrees about or has outlived"]
    if len(found["competing"]) > 0:
        lines.extend(["", "## Files with more than one decision that holds", ""])
        for each in found["competing"]:
            lines.append(f"- {each['path']}")
            for decision in each["decisions"]:
                lines.append(f"  - {decision['text']} [{decision['id']}, from {decision['source']}]")
    if len(found["duplicates"]) > 0:
        lines.extend(["", "## The same thing remembered more than once", ""])
        for group in found["duplicates"]:
            ids: list[str] = []
            for entry in group:
                ids.append(entry["id"])
            lines.append(f"- {group[0]['text']} [{', '.join(ids)}]")
    if len(found["restated"]) > 0:
        lines.extend(["", "## Rules the user gave more than once, candidates for CLAUDE.md", ""])
        for group in found["restated"]:
            lines.append(f"- given {len(group)} times:")
            for entry in group:
                lines.append(f"  - {entry['text']} [from {entry['source']}, {entry['id']}]")
    if len(found["stale"]) > 0:
        lines.extend(["", "## May be out of date, and given to agents again only once the recheck job clears it or it is the user's", ""])
        for each in found["stale"]:
            lines.append(cited(each["item"]))
            for reason in each["reasons"]:
                lines.append(f"  - {reason}")
    lines.extend(section("Decisions whose files are all gone", found["gone"]))
    lines.extend(section("Decisions that say when to revisit them", found["conditional"]))
    if len(lines) == 1:
        lines.extend(["", "Nothing to consolidate."])
    return "\n".join(lines) + "\n"


def day(stamp: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def past_lines(past: PastData) -> list[str]:
    lines: list[str] = []
    if past["first"] is not None and past["last"] is not None:
        lines.extend(["", "## In git", "", f"Changed in {past['commits']} commits, from {day(past['first'])} to {day(past['last'])}."])
        if len(past["names"]) > 0:
            lines.append(f"Earlier names, newest first: {', '.join(past['names'])}.")
        if len(past["together"]) > 0:
            parts: list[str] = []
            for each in past["together"]:
                parts.append(f"{each['path']} ({each['count']})")
            lines.append(f"Changed in the same commits: {', '.join(parts)}.")
    return lines


def file_markdown(page: FileData) -> str:
    lines: list[str] = [f"# {page['path']}"]
    lines.extend(section("Rules given", page["rules"]))
    lines.extend(decision_lines(page["open"], "Decisions that hold"))
    lines.extend(section("Turned down", page["rejected"]))
    lines.extend(section("Learned", page["learned"]))
    lines.extend(touch_lines("Changed by", page["changes"]))
    lines.extend(touch_lines("Read by", page["reads"]))
    lines.extend(chain_lines(page["history"]))
    lines.extend(decision_lines(page["superseded"], "Superseded decisions"))
    lines.extend(section("Not verified", page["unverified"]))
    if len(lines) == 1:
        lines.extend(["", "Nothing recorded touches it."])
    lines.extend(past_lines(page["past"]))
    if page["gone"]:
        lines[1:1] = ["", "No longer in the tree."]
    return "\n".join(lines) + "\n"


def headed(title: str, lines: Sequence[str]) -> list[str]:
    if len(lines) == 0:
        return []
    elif len(lines) > PARTS:
        return ["", f"## {title}", "", *lines[:PARTS], f"- and {len(lines) - PARTS} more"]
    else:
        return ["", f"## {title}", "", *lines]


def module_sentence(path: str, module: ModuleData) -> str:
    if module["path"] == path:
        return f"It is the module {module['path']} ({', '.join(module['manifests'])})."
    else:
        return f"It is in the module {module['path']} ({', '.join(module['manifests'])})."


def module_lines(page: RegionData) -> list[str]:
    module = page["module"]
    if module is None:
        return []
    else:
        lines = ["", module_sentence(page["path"], module)]
        if len(module["depends"]) > 0:
            lines.append(f"That module depends on {', '.join(module['depends'])}.")
        if len(module["used_by"]) > 0:
            lines.append(f"These depend on it: {', '.join(module['used_by'])}.")
        return lines


def note_lines(page: RegionData) -> list[str]:
    note = page["note"]
    if note is None:
        return ["", f"lain's map has no page for it yet ({page['written']} of {page['regions']} regions are written); the map job writes them."]
    else:
        lines = ["", "## What it is", ""]
        if note["path"] != page["path"]:
            lines.extend([f"From the page for {region_name(note['path'])}, the region it is in:", ""])
        lines.extend([note["summary"], "", note["text"], ""])
        marks = [f"Written on {day(note['time'])}"]
        if not note["fresh"]:
            marks.append("before its code last changed, so it may be out of date")
        if len(note["unchecked"]) > 0:
            marks.append(f"naming what no file under it has: {', '.join(note['unchecked'])}")
        lines.append(f"{', '.join(marks)}.")
        return lines


def part_line(part: PartData) -> str:
    match part["kind"]:
        case "region" if part["summary"] != "":
            return f"- {part['path']}: {part['summary']}"
        case "region":
            return f"- {part['path']} ({part['files']} files, no page yet)"
        case "directory":
            return f"- {part['path']} ({part['files']} files)"
        case "file":
            return f"- {part['path']} ({part['lines']} lines)"


def count_line(each: CountData, single: bool) -> str:
    if single:
        return f"- {each['path']}"
    else:
        return f"- {each['path']} ({each['count']})"


def region_title(page: RegionData) -> str:
    if page["kind"] == "project":
        return "The project"
    else:
        return page["path"]


def region_markdown(page: RegionData) -> str:
    lines: list[str] = [f"# {region_title(page)}"]
    if page["gone"]:
        lines.extend(["", "No longer in the tree."])
    lines.extend(module_lines(page))
    lines.extend(note_lines(page))
    parts: list[str] = []
    for part in page["parts"]:
        parts.append(part_line(part))
    lines.extend(headed("Parts", parts))
    declared: list[str] = []
    for symbol in page["symbols"]:
        declared.append(f"- {symbol['kind']} {symbol['name']}, line {symbol['line']}: {symbol['signature']}")
    lines.extend(headed("Declared here", declared))
    single = page["kind"] == "file"
    for title, counts, alone in (("Imports", page["uses"], single), ("Imported by", page["used_by"], single), ("Changed in the same commits", page["together"], False)):
        shown: list[str] = []
        for each in counts:
            shown.append(count_line(each, alone))
        lines.extend(headed(title, shown))
    lines.extend(section("Rules given", page["rules"]))
    lines.extend(decision_lines(page["open"], "Decisions that hold"))
    lines.extend(section("Learned", page["learned"]))
    lines.extend(section("Not verified", page["unverified"]))
    if page["left"] > 0:
        lines.extend(["", f"{page['left']} more memories hold here and are left out; the file pages and decisions list them."])
    return "\n".join(lines) + "\n"


def hit_lines(found: SearchData) -> list[str]:
    lines: list[str] = []
    if len(found["unmatched"]) > 0:
        lines.append(f"nothing recorded mentions: {', '.join(found['unmatched'])}")
    for hit in found["hits"]:
        lines.append(f"{hit['score']:.1f} {hit['kind']} {hit['ref']}: {clipped(hit['title'], TITLE)} ({hit['detail']})")
    if found["more"] > 0:
        lines.append(f"{found['more']} more match less well and are left out; more words narrow it")
    return lines


def clipped(text: str, limit: int) -> str:
    if len(text) > limit:
        return f"{text[:limit]}..."
    else:
        return text
