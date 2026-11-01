from __future__ import annotations

import ast
import bisect
import datetime
import json
import re
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, TypedDict, cast

from config import Settings, reviewing
from extract import Extractor
from history import Brief, History, Note
from memory import Memory, Store, lead, present, scoped
from model import Failure, Usage
from page import Builder, Page, Unreadable, pages, read, real, when, write
from places import CHECKS, LAIN, Places
from prompts import message, prompt
from skeleton import function_bodies, language
from spans import chunk, here, span
from spec import RECHECK, RecheckArgs
from store import locked
from tree import hashed

IDENTIFIER = re.compile(r"`([^`\n]+)`|\b(\w+(?:\.\w+)*\(\))|\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b|\b([A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+)\b")
DOTTED = re.compile(r"[A-Za-z_][\w.]*")
CLAUSE = re.compile(r"[.;](?:\s|$)|\n")
GONE = re.compile(r"\b(?:remov|delet|drop|strip|retir|replac|undid|undo|rename|no longer|instead of|rather than)", re.IGNORECASE)
EXTENSION = re.compile(r"\.[A-Za-z]")
WATCHED = ("decision", "option", "observation")
NAMED = ("decision", "observation")
BASELINES = "baselines"
TRIGGER = re.compile(r"^\s*(file|date|dependency)\s*:\s*(.+?)\s*$", re.IGNORECASE)
DEPENDENCY = re.compile(r"^([\w.@/-]+)\s*(?:(>=|>|<=|<|==)\s*(\d[\w.]*))?$")
MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "package.json")

type TriggerKind = Literal["file", "date", "dependency"]


class BaselineData(TypedDict):
    id: str
    names: list[str]
    absent: list[str]
    bodies: dict[str, str]


@dataclass(frozen=True)
class Named:
    name: str
    strong: bool


@dataclass(frozen=True)
class Flagged:
    memory: Memory
    reasons: tuple[str, ...]


def named(token: str) -> str | None:
    bare = token.removesuffix("()")
    head, dot, last = bare.rpartition(".")
    if DOTTED.fullmatch(bare) is None:
        return None
    elif dot == "" or token.endswith("()") or head[:1].isupper():
        return last
    else:
        return None


def names_in(text: str) -> list[Named]:
    found: dict[str, bool] = {}
    for clause in CLAUSE.split(text):
        if GONE.search(clause) is None:
            for match in IDENTIFIER.finditer(clause):
                quoted, call, snake, camel = match.groups()
                pathed = clause[match.start() - 1 : match.start()] == "/" or EXTENSION.match(clause, match.end()) is not None
                name = named(quoted or call or snake or camel or "")
                if name is not None and (quoted is not None or call is not None or not pathed):
                    found[name] = found.get(name, False) or camel is None
    kept: list[Named] = []
    for name, strong in found.items():
        kept.append(Named(name, strong))
    return kept


def trigger_kind(text: str) -> TriggerKind:
    match text.lower():
        case "file":
            return "file"
        case "date":
            return "date"
        case _:
            return "dependency"


def mentions(name: str, text: str) -> bool:
    return re.search(rf"\b{re.escape(name)}\b", text) is not None


def bodies_of(text: str) -> dict[str, str]:
    try:
        parsed = ast.parse(text)
    except (SyntaxError, ValueError):
        return {}
    else:
        found: dict[str, str] = {}
        for node in ast.walk(parsed):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name not in found:
                segment = ast.get_source_segment(text, node)
                if segment is not None:
                    found[node.name] = hashed(segment.encode())
        return found


def bodies_in(path: str, text: str) -> dict[str, str]:
    if path.endswith(".py"):
        return bodies_of(text)
    else:
        return function_bodies(path, text)


def declared(text: str, name: str) -> str | None:
    found = re.search(rf"""(?im)^\s*["']?{re.escape(name)}["']?\s*[=:]?\s*["']?([~^<>=!]*\s*\d[\w.*+-]*)""", text)
    if found is None:
        found = re.search(rf"""(?im)["']{re.escape(name)}\s*([~^<>=!]+\s*\d[\w.*+-]*)""", text)
    if found is None:
        return None
    else:
        return found.group(1).replace(" ", "")


def version(text: str) -> tuple[int, ...]:
    found: list[int] = []
    for part in re.findall(r"\d+", text)[:3]:
        found.append(int(part))
    return tuple(found)


def reached(spec: str, operator: str, wanted: str) -> bool:
    have = version(spec)
    want = version(wanted)
    match operator:
        case ">=":
            return have >= want
        case ">":
            return have > want
        case "<=":
            return have <= want
        case "<":
            return have < want
        case _:
            return have == want


def git(root: Path, args: list[str], given: bytes) -> bytes | None:
    done = subprocess.run(["git", *args], cwd=root, input=given, capture_output=True)
    if done.returncode == 0:
        return done.stdout
    else:
        return None


def times_of(history: Sequence[tuple[float, str]]) -> list[float]:
    found: list[float] = []
    for stamp, _ in history:
        found.append(stamp)
    return found


def mentioned_in(name: str, texts: Sequence[str]) -> bool:
    for text in texts:
        if mentions(name, text):
            return True
    return False


def commits(root: Path) -> list[tuple[float, str]]:
    found: list[tuple[float, str]] = []
    if (root / ".git").exists():
        shown = git(root, ["log", "--format=%ct %H", "HEAD"], b"")
        if shown is not None:
            for line in shown.decode().splitlines():
                stamp, _, sha = line.partition(" ")
                found.append((float(stamp), sha))
    return sorted(found)


def blobs(root: Path, keys: Sequence[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    if len(keys) > 0:
        asked: list[str] = []
        for key in keys:
            asked.append(f"{key}\n")
        shown = git(root, ["cat-file", "--batch"], "".join(asked).encode())
        if shown is not None:
            at = 0
            for key in keys:
                end = shown.index(b"\n", at)
                header = shown[at:end].decode(errors="replace").split(" ")
                at = end + 1
                if len(header) == 3 and header[2].isdigit():
                    size = int(header[2])
                    if header[1] == "blob":
                        found[key] = shown[at : at + size].decode(errors="replace")
                    at += size + 1
    return found


def anywhere(root: Path, names: set[str]) -> set[str]:
    found: set[str] = set()
    if len(names) > 0 and (root / ".git").exists():
        args = ["grep", "-o", "-w", "-h", "-F", "-I", "--untracked"]
        for name in sorted(names):
            args.extend(["-e", name])
        args.extend(["--", ".", f":(exclude){LAIN}"])
        done = subprocess.run(["git", *args], cwd=root, capture_output=True)
        for line in done.stdout.decode(errors="replace").splitlines():
            found.add(line.strip())
    return found


class Staleness:
    root: Path
    path: Path
    store: Store
    known: dict[str, BaselineData]
    checked: dict[str, float]
    texts: dict[str, str | None]
    parsed: dict[str, dict[str, str]]
    changed: dict[str, float]
    history: list[tuple[float, str]] | None

    def __init__(self, root: Path, home: Path, store: Store) -> None:
        self.root = root
        self.path = home / BASELINES
        self.store = store
        self.known = {}
        self.checked = {}
        self.texts = {}
        self.parsed = {}
        self.changed = {}
        self.history = None
        for path in pages(self.path):
            try:
                page = read(path)
            except Unreadable:
                path.unlink()
            else:
                base = baseline_of(page)
                self.known[base["id"]] = base
                held = page.one("checked")
                if held is not None:
                    self.checked[base["id"]] = real(held)
        unseen: list[Memory] = []
        for memory in store.ordered():
            if memory.kind in WATCHED and memory.id not in self.known:
                unseen.append(memory)
        if len(unseen) > 0:
            self.first_look(unseen)

    def keep(self, data: BaselineData) -> None:
        self.known[data["id"]] = data
        write(self.path / f"{data['id']}.md", baseline_page(data, self.checked.get(data["id"])))

    def text(self, path: str) -> str | None:
        if path not in self.texts:
            target = self.root / path
            self.texts[path] = None
            if target.is_file():
                self.texts[path] = target.read_text(errors="replace")
        return self.texts[path]

    def bodies(self, path: str) -> dict[str, str]:
        if path not in self.parsed:
            text = self.text(path)
            self.parsed[path] = {}
            if text is not None and language(path) is not None:
                self.parsed[path] = bodies_in(path, text)
        return self.parsed[path]

    def plain(self, memory: Memory) -> list[str]:
        found: list[str] = []
        for path in self.store.current_files(memory):
            if not scoped(path):
                found.append(path)
        return found

    def first_look(self, memories: Sequence[Memory]) -> None:
        history = commits(self.root)
        times = times_of(history)
        revs: dict[str, str] = {}
        keys: list[str] = []
        for memory in memories:
            index = bisect.bisect_right(times, memory.time) - 1
            if index >= 0:
                revs[memory.id] = history[index][1]
                for path in self.plain(memory):
                    keys.append(f"{history[index][1]}:{path}")
        then = blobs(self.root, sorted(set(keys)))
        looked: list[tuple[Memory, list[Named], list[str], dict[str, str]]] = []
        candidates: set[str] = set()
        for memory in memories:
            past: dict[str, str] = {}
            for path in self.plain(memory):
                old = then.get(f"{revs.get(memory.id, '')}:{path}")
                if old is not None:
                    past[path] = old
            names = self.watched_names(memory)
            had = self.had(memory, names, past)
            looked.append((memory, names, had, past))
            for each in names:
                if each.strong and each.name not in had:
                    candidates.add(each.name)
        found = anywhere(self.root, candidates)
        for memory, names, had, past in looked:
            absent: list[str] = []
            for each in names:
                if each.strong and each.name not in had and each.name not in found:
                    absent.append(each.name)
            self.keep({"id": memory.id, "names": had, "absent": absent, "bodies": self.first_bodies(memory, had, past)})

    def watched_names(self, memory: Memory) -> list[Named]:
        if memory.kind in NAMED:
            return names_in(memory.text)
        else:
            return []

    def had(self, memory: Memory, names: Sequence[Named], past: dict[str, str]) -> list[str]:
        texts: list[str] = list(past.values())
        for path in self.plain(memory):
            text = self.text(path)
            if text is not None:
                texts.append(text)
        found: list[str] = []
        for each in names:
            if mentioned_in(each.name, texts):
                found.append(each.name)
        return found

    def first_bodies(self, memory: Memory, had: Sequence[str], past: dict[str, str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in self.plain(memory):
            if language(path) is not None:
                earlier: dict[str, str] = {}
                if path in past:
                    earlier = bodies_in(path, past[path])
                for name in had:
                    digest = earlier.get(name, self.bodies(path).get(name))
                    if digest is not None and name not in found:
                        found[name] = digest
        return found

    def flags(self, memory: Memory) -> tuple[list[str], list[str]]:
        hiding: list[str] = []
        marks: list[str] = []
        base = self.known.get(memory.id)
        if memory.kind in WATCHED and base is not None:
            plain = self.plain(memory)
            existing: list[str] = []
            texts: list[str] = []
            for path in plain:
                text = self.text(path)
                if text is not None:
                    existing.append(path)
                    texts.append(text)
            if len(plain) > 0 and len(existing) == 0:
                hiding.append(message("stale_gone", files=", ".join(plain)))
            elif len(plain) > 0:
                missing: list[str] = []
                for name in base["names"]:
                    if not mentioned_in(name, texts):
                        missing.append(name)
                if len(missing) > 0:
                    hiding.append(message("stale_names", names=", ".join(missing), files=", ".join(existing)))
                changed: list[str] = []
                for name, digest in base["bodies"].items():
                    now: list[str] = []
                    for path in existing:
                        if name in self.bodies(path):
                            now.append(self.bodies(path)[name])
                    if name not in missing and len(now) > 0 and digest not in now:
                        changed.append(name)
                if len(changed) > 0:
                    marks.append(message("stale_body", names=", ".join(changed)))
            if len(base["absent"]) > 0:
                hiding.append(message("stale_absent", names=", ".join(base["absent"])))
        marks.extend(self.fired(memory))
        return hiding, marks

    def fired(self, memory: Memory) -> list[str]:
        since = max(memory.time, self.checked.get(memory.id, memory.time))
        found: list[str] = []
        for condition in memory.conditions.revisit_when:
            matched = TRIGGER.match(condition)
            if matched is not None:
                reason = self.trigger(trigger_kind(matched.group(1)), matched.group(2), since)
                if reason is not None:
                    found.append(message("stale_trigger", condition=condition.strip(), reason=reason))
        return found

    def trigger(self, kind: TriggerKind, value: str, since: float) -> str | None:
        match kind:
            case "file":
                if not (self.root / value).exists():
                    return message("trigger_gone")
                elif self.last_change(value) > since:
                    return message("trigger_changed")
                else:
                    return None
            case "date":
                try:
                    day = datetime.date.fromisoformat(value)
                except ValueError:
                    return None
                else:
                    due = datetime.datetime.combine(day, datetime.time()).timestamp()
                    if time.time() >= due > since:
                        return message("trigger_date")
                    else:
                        return None
            case "dependency":
                return self.dependency(value, since)

    def last_change(self, path: str) -> float:
        if path not in self.changed:
            self.changed[path] = 0.0
            if (self.root / ".git").exists():
                shown = git(self.root, ["log", "-1", "--format=%ct", "HEAD", "--", path], b"")
                if shown is not None and shown.strip() != b"":
                    self.changed[path] = float(shown.decode().strip())
        return self.changed[path]

    def declared_now(self, name: str) -> str | None:
        for manifest in MANIFESTS:
            text = self.text(manifest)
            if text is not None:
                found = declared(text, name)
                if found is not None:
                    return found
        return None

    def declared_then(self, name: str, since: float) -> str | None:
        if self.history is None:
            self.history = commits(self.root)
        index = bisect.bisect_right(times_of(self.history), since) - 1
        if index < 0:
            return None
        else:
            rev = self.history[index][1]
            keys: list[str] = []
            for manifest in MANIFESTS:
                keys.append(f"{rev}:{manifest}")
            old = blobs(self.root, keys)
            for manifest in MANIFESTS:
                text = old.get(f"{rev}:{manifest}")
                if text is not None:
                    found = declared(text, name)
                    if found is not None:
                        return found
            return None

    def dependency(self, value: str, since: float) -> str | None:
        parsed = DEPENDENCY.match(value)
        if parsed is None:
            return None
        else:
            name, operator, wanted = parsed.groups()
            now = self.declared_now(name)
            then = self.declared_then(name, since)
            if operator is not None and wanted is not None:
                if now is not None and reached(now, operator, wanted) and (then is None or not reached(then, operator, wanted)):
                    return message("trigger_version", spec=now)
                else:
                    return None
            else:
                if then is not None and now != then:
                    return message("trigger_moved", then=then, now=now or message("trigger_removed"))
                else:
                    return None

    def fresh(self, memory: Memory) -> bool:
        return memory.basis == "user" or len(self.flags(memory)[0]) == 0

    def notes(self, memory: Memory) -> list[str]:
        hiding, marks = self.flags(memory)
        return hiding + marks

    def flagged(self) -> list[Flagged]:
        found: list[Flagged] = []
        for memory in self.store.ordered():
            if self.store.wanted(memory, None):
                reasons = self.notes(memory)
                if len(reasons) > 0:
                    found.append(Flagged(memory, tuple(reasons)))
        return found

    def clear(self, memory: Memory) -> None:
        had = self.had(memory, self.watched_names(memory), {})
        bodies: dict[str, str] = {}
        for path in self.plain(memory):
            for name in had:
                digest = self.bodies(path).get(name)
                if digest is not None:
                    bodies[name] = digest
        self.checked[memory.id] = time.time()
        self.keep({"id": memory.id, "names": had, "absent": [], "bodies": bodies})


def baseline_page(data: BaselineData, checked: float | None) -> Page:
    built = Builder(f"what the files of memory {data['id']} held when lain first looked")
    built.field("id", data["id"])
    if checked is not None:
        built.field("checked", when(checked))
    for name in data["names"]:
        built.field("name", name)
    for name in data["absent"]:
        built.field("absent", name)
    for name, seen in sorted(data["bodies"].items()):
        built.field("body", f"{seen} {name}")
    return built.page()


def baseline_of(page: Page) -> BaselineData:
    bodies: dict[str, str] = {}
    for line in page.every("body"):
        seen, _, name = line.partition(" ")
        bodies[name] = seen
    return {"id": page.need("id"), "names": page.every("name"), "absent": page.every("absent"), "bodies": bodies}


def staleness(found: Places, store: Store) -> Staleness:
    return Staleness(found.repo.root, found.memory, store)


@dataclass(frozen=True)
class Rechecked:
    tried: tuple[str, ...]
    failed: tuple[str, ...]


def recheck(
    found: Places, settings: Settings, usage: Usage, say: Callable[[str], None], pick: Callable[[list[Flagged]], list[Flagged]], stop: threading.Event
) -> Rechecked:
    store = Store(found.memory)
    watch = staleness(found, store)
    light = replace(reviewing(settings), effort=settings.light_effort)
    extractor = Extractor(found, light, usage)
    root = found.repo.root
    access = extractor.access()
    system = prompt("recheck", root=".")
    home = found.agent(CHECKS)
    tried: list[str] = []
    failed: list[str] = []
    with locked(home):
        history = History(home)
        for each in pick(watch.flagged()):
            memory = each.memory
            if stop.is_set():
                pass
            elif memory.basis == "user":
                say(message("recheck_user", id=memory.id, reasons="; ".join(each.reasons)))
            else:
                listed = ", ".join(store.current_files(memory))
                text = message("recheck_item", lead=lead(memory, ()), files=listed, reasons="\n".join(each.reasons))
                tried.append(memory.id)
                history.append(Brief(text, "recheck", system, RECHECK.name))
                with span("recheck", memory.id, str(root), here(), {"text": memory.text, "reasons": "\n".join(each.reasons)}) as doing:
                    answer = extractor.explore(system, text, RECHECK, access, memory.id, light)
                    if not isinstance(answer, Failure):
                        first = cast(RecheckArgs, answer.answer)
                        if first["verdict"] == "changed" and first["text"].strip() == "":
                            answer = extractor.again(answer, message("recheck_no_text"), RECHECK, access, light)
                    if isinstance(answer, Failure):
                        failed.append(memory.id)
                        doing.ok = False
                        said = f"{memory.id} not checked, and checked again next time: {answer.reason}"
                    else:
                        args = cast(RecheckArgs, answer.answer)
                        reason = args["reason"].strip()
                        if args["verdict"] == "changed" and args["text"].strip() == "":
                            failed.append(memory.id)
                            doing.ok = False
                            said = f"{memory.id} not changed, and checked again next time: the check said changed but gave no text"
                        else:
                            note = history.append(Note(json.dumps(args, ensure_ascii=False)))
                            source = f"{home.name}/{note.number}"
                            match args["verdict"]:
                                case "holds":
                                    watch.clear(memory)
                                case "outdated":
                                    store.retract(memory, reason, "inferred", source, note.time, (reason,))
                                case "changed":
                                    files: list[str] = []
                                    for path in args["files"]:
                                        if path.strip() != "" and present(root, path.strip()):
                                            files.append(path.strip())
                                    if len(files) == 0:
                                        files = list(store.current_files(memory))
                                    store.correct(memory, args["text"].strip(), files, memory.conditions, "inferred", source, note.time, (reason,))
                            said = f"{memory.id} {args['verdict']}: {reason}"
                    chunk(doing, "note", said)
                    say(said)
    return Rechecked(tuple(tried), tuple(failed))
