from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from atlas import NoteData, changed_since, notes_in, region_note
from config import Settings, address
from extract import Extractor, extract_session
from ingest import TRACES, ChangeTrace, SeenTrace, TraceData, began, ended, ingest, relative, session_name, write_trace
from memory import Memory, Store, lead, standing
from model import Usage
from page import Builder, Page, Unreadable, dated, items, moment, pages, read, real, when, write, write_items
from places import LAIN, LAUNCHER, Places, Repo, in_checkout, places
from post import Address, Office, address_of, delivered
from prompts import message
from stale import Staleness, staleness
from store import JSON, locked, replace_text
from tree import file_diff, hashed
from watch import Touched, Watch, watch

WORK = "work"
HEAVY = ("Stop", "SubagentStop", "PreCompact", "SessionEnd")
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
RECENT = "edits"
SEEN = "seen"
SHOWN = 40
SHOWN_CHARS = 2500
CHANGED_SHOWN = 12
ROOM = 4000
FILE_ROOM = 1500
MAP_ROOM = 800
LEAD_CHARS = 240
LINGER = 600.0
PLACED = "placed.md"
GIVEN = "given.md"
TOLD = "told.md"
WARNED = "warned.md"


class HookInput(TypedDict, total=False):
    session_id: str
    transcript_path: str
    cwd: str
    hook_event_name: str
    tool_name: str
    tool_input: dict[str, JSON]
    tool_use_id: str
    tool_response: JSON
    duration_ms: int
    prompt: str
    claude_pid: int


class EditData(TypedDict):
    session: str
    root: str
    path: str
    time: float
    change: str


@dataclass(frozen=True)
class Context:
    places: Places
    session: str
    data: HookInput

    @property
    def repo(self) -> Repo:
        return self.places.repo

    @property
    def home(self) -> Path:
        return self.places.agent(self.session)

    def store(self) -> Store:
        return Store(self.places.memory)


def context(data: HookInput) -> Context:
    found = places(Path(data["cwd"]).resolve())
    return Context(found, session_name(found, data["session_id"], time.time()), data)


def answered(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}})


def edited_path(data: HookInput) -> str | None:
    given = data.get("tool_input", {})
    for key in ("file_path", "notebook_path"):
        value = given.get(key)
        if isinstance(value, str):
            return value
    return None


def mapped_text(text: str) -> str:
    if len(text) > MAP_ROOM:
        return f"{text[:MAP_ROOM]}..."
    else:
        return text


def start_map(ctx: Context) -> list[str]:
    note = notes_in(ctx.places).get(".")
    if note is None:
        return []
    else:
        with locked(ctx.home):
            write_items(ctx.home / PLACED, "map pages this session was given", "region", ["."])
        return [mapped_text(message("hook_map_start", day=moment(note["time"])[:10], summary=note["summary"], text=note["text"]))]


def placed(ctx: Context, region: str) -> bool:
    with locked(ctx.home):
        given = items(ctx.home / PLACED, "region")
        if region in given:
            return False
        else:
            given.append(region)
            write_items(ctx.home / PLACED, "map pages this session was given", "region", given)
            return True


def changed_mark(ctx: Context, note: NoteData, path: str) -> str:
    if changed_since(ctx.repo.root, note, path):
        return message("hook_map_changed")
    else:
        return ""


def file_map(ctx: Context, path: str) -> list[str]:
    note = region_note(ctx.places, path)
    if note is not None and placed(ctx, note["path"]):
        mark = changed_mark(ctx, note, path)
        day = moment(note["time"])[:10]
        return [mapped_text(message("hook_map_file", path=path, region=note["path"], day=day, mark=mark, summary=note["summary"], text=note["text"]))]
    else:
        return []


def fresh_leads(ctx: Context, settings: Settings, path: str, room: int) -> list[str]:
    with locked(ctx.home):
        given = items(ctx.home / GIVEN, "file")
        if path in given:
            return []
        else:
            given.append(path)
            write_items(ctx.home / GIVEN, "files this session was given leads on", "file", given)
            told = items(ctx.home / TOLD, "memory")
            store = ctx.store()
            watching = staleness(ctx.places, store)
            found: list[Memory] = []
            for memory in store.ranked(store.relevant([path], watching), path):
                if memory.id not in told:
                    found.append(memory)
            given = fitted(found[: settings.lead_limit], watching, room)
            lines: list[str] = []
            for memory, line in given:
                lines.append(line)
                told.append(memory.id)
            if len(found) > len(given):
                lines.append(message("leads_left_out", count=str(len(found) - len(given))))
            write_items(ctx.home / TOLD, "memories this session was told", "memory", told)
            return lines


def short_lead(memory: Memory, notes: Sequence[str]) -> str:
    text = " ".join(memory.text.split())
    if len(text) > LEAD_CHARS:
        text = f"{text[:LEAD_CHARS]}..."
    line = f"- {memory.kind} {memory.id} ({memory.basis}, {memory.source}): {text}"
    if len(notes) > 0:
        return f"{line} [may be out of date]"
    else:
        return line


def fitted(memories: Sequence[Memory], watching: Staleness, room: int) -> list[tuple[Memory, str]]:
    found: list[tuple[Memory, str]] = []
    used = 0
    for memory in memories:
        line = short_lead(memory, watching.notes(memory))
        used += len(line) + 1
        if used > room:
            return found
        else:
            found.append((memory, line))
    return found


def newest_first(memory: Memory) -> tuple[tuple[int, int], float]:
    return standing(memory), -memory.time


def session_start(ctx: Context, settings: Settings) -> str | None:
    if settings.leads:
        return told("SessionStart", start_parts(ctx, settings))
    else:
        return None


def start_parts(ctx: Context, settings: Settings) -> list[str]:
    store = ctx.store()
    watching = staleness(ctx.places, store)
    users: list[Memory] = []
    others: list[Memory] = []
    for memory in store.relevant(None, watching):
        if memory.basis == "user":
            users.append(memory)
        else:
            others.append(memory)
    others.sort(key=newest_first)
    parts = start_map(ctx)
    leads: list[str] = []
    ids: list[str] = []
    given = fitted(users + others[: settings.start_leads], watching, ROOM - length(parts))
    for memory, line in given:
        leads.append(line)
        ids.append(memory.id)
    with locked(ctx.home):
        write_items(ctx.home / TOLD, "memories this session was told", "memory", ids)
    if len(users) + len(others) > len(given):
        leads.append(message("hook_leads_more", count=str(len(users) + len(others) - len(given))))
    if len(leads) > 0:
        parts.append(message("hook_leads_start", bases=message("lead_bases"), leads="\n".join(leads)))
    return parts


def file_leads(ctx: Context, settings: Settings, path: str) -> list[str]:
    if settings.leads:
        parts = file_map(ctx, path)
        leads = fresh_leads(ctx, settings, path, FILE_ROOM)
        if len(leads) > 0:
            parts.append(message("hook_leads_file", path=path, bases="", leads="\n".join(leads)))
        return parts
    else:
        return []


def length(parts: Sequence[str]) -> int:
    total = 0
    for part in parts:
        total += len(part)
    return total


def told(event: str, parts: list[str]) -> str | None:
    if len(parts) == 0:
        return None
    else:
        return answered(event, "\n\n".join(parts))


def clipped(text: str) -> str:
    lines = text.rstrip("\n").split("\n")
    kept: list[str] = []
    used = 0
    for line in lines[:SHOWN]:
        used += len(line) + 1
        if used > SHOWN_CHARS:
            break
        else:
            kept.append(line)
    if len(kept) < len(lines):
        return "\n".join([*kept, message("hook_edit_more", count=str(len(lines) - len(kept)))])
    else:
        return "\n".join(lines)


def snippet_diff(path: str, old: str, new: str) -> str:
    return file_diff(path, old.removesuffix("\n") + "\n", new.removesuffix("\n") + "\n")


def change_of(tool: str, given: dict[str, JSON], path: str) -> str:
    old = given.get("old_string")
    new = given.get("new_string")
    edits = given.get("edits")
    content = given.get("content")
    if tool == "Edit" and isinstance(old, str) and isinstance(new, str):
        return clipped(snippet_diff(path, old, new))
    elif tool == "MultiEdit" and isinstance(edits, list):
        parts: list[str] = []
        for each in edits:
            if isinstance(each, dict):
                before = each.get("old_string")
                after = each.get("new_string")
                if isinstance(before, str) and isinstance(after, str):
                    parts.append(snippet_diff(path, before, after))
        return clipped("".join(parts))
    elif tool == "Write" and isinstance(content, str):
        return message("hook_edit_written", count=str(content.count("\n")))
    else:
        return message("hook_edit_other", tool=tool)


def seen_at(ctx: Context, path: str) -> Path:
    return ctx.home / SEEN / f"{path}.md"


def saw(ctx: Context, path: str) -> None:
    data = text_file(ctx.repo.root / path)
    target = seen_at(ctx, path)
    if data is not None:
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target, Page(f"{path} as this session last read or wrote it", (("file", path),), (("content", data.decode(errors="replace")),)))
    elif target.is_file():
        target.unlink()


def outside(ctx: Context, path: str) -> list[str]:
    target = seen_at(ctx, path)
    if not target.is_file():
        return []
    else:
        before = read(target).value("content")
        data = text_file(ctx.repo.root / path)
        now = ""
        if data is not None:
            now = data.decode(errors="replace")
        if now == before:
            return []
        else:
            saw(ctx, path)
            return [message("hook_changed_outside", path=path, change=clipped(file_diff(path, before, now)))]


def edit_page(entry: EditData) -> Page:
    built = Builder(f"{entry['session']} edited {entry['path']} at {moment(entry['time'])}")
    built.field("session", entry["session"])
    built.field("root", entry["root"])
    built.field("path", entry["path"])
    built.field("time", when(entry["time"]))
    built.section("change", entry["change"])
    return built.page()


def recent(found: Places) -> list[EditData]:
    kept: list[EditData] = []
    for path in pages(found.home / RECENT):
        try:
            page = read(path)
            kept.append({"session": page.need("session"), "root": page.need("root"), "path": page.need("path"), "time": real(page.need("time")), "change": page.value("change")})
        except (Unreadable, FileNotFoundError):
            pass
    return kept


def note_edit(ctx: Context, path: str, window: float) -> None:
    now = time.time()
    change = change_of(ctx.data.get("tool_name", ""), ctx.data.get("tool_input", {}), path)
    entry: EditData = {"session": ctx.session, "root": str(ctx.repo.root), "path": path, "time": now, "change": change}
    folder = ctx.places.home / RECENT
    with locked(folder):
        for old in pages(folder):
            if now - old.stat().st_mtime > window:
                old.unlink()
        target = folder / f"{dated(now)}.md"
        count = 1
        while target.exists():
            count += 1
            target = folder / f"{dated(now)}-{count}.md"
        write(target, edit_page(entry))


def decided_by(entry: EditData) -> list[str]:
    root = Path(entry["root"])
    lines: list[str] = []
    if root.is_dir():
        store = Store(places(root).memory)
        for memory in store.open_decisions([entry["path"]]):
            if memory.agent == entry["session"] or memory.agent.startswith(f"{entry['session']}/"):
                lines.append(lead(memory, ()))
    return lines


def elsewhere(ctx: Context, path: str, window: float) -> list[str]:
    now = time.time()
    latest: dict[str, EditData] = {}
    with locked(ctx.home):
        warned = items(ctx.home / WARNED, "edit")
        for entry in recent(ctx.places):
            key = f"{entry['session']} {entry['time']!r}"
            if entry["session"] != ctx.session and entry["path"] == path and now - entry["time"] < window and key not in warned:
                warned.append(key)
                latest[entry["session"]] = entry
        if len(latest) > 0:
            write_items(ctx.home / WARNED, "other sessions' edits this session was warned of", "edit", warned)
    parts: list[str] = []
    for entry in latest.values():
        minutes = str(round((now - entry["time"]) / 60))
        shown = message("hook_edited_elsewhere", session=entry["session"], path=path, minutes=minutes, root=entry["root"], change=entry["change"])
        decisions = decided_by(entry)
        if len(decisions) > 0:
            shown = "\n".join([shown, message("hook_edited_decided", path=path), *decisions])
        parts.append(shown)
    return parts


def limit_of(data: HookInput) -> float | None:
    given = data.get("tool_input", {})
    limit = given.get("timeout")
    if not isinstance(limit, int):
        limit = 120000
    if given.get("run_in_background") is True:
        return None
    else:
        return limit / 1000


@dataclass
class Watching:
    watcher: Watch
    root: Path
    begun: float
    until: float
    session: str
    ended: float | None = None
    changed: frozenset[str] = frozenset()


class Threaded:
    watching: dict[str, Watching]
    finished: list[Watching]
    lock: threading.Lock

    def __init__(self) -> None:
        self.watching = {}
        self.finished = []
        self.lock = threading.Lock()

    def start(self, root: Path, session: str, data: HookInput) -> None:
        seconds = limit_of(data)
        if seconds is not None:
            watcher = watch(root)
            if watcher is not None:
                now = time.time()
                with self.lock:
                    self.watching[data.get("tool_use_id", "")] = Watching(watcher, root, now, now + seconds, session)

    def overlapping(self, mine: Watching) -> list[Watching]:
        found: list[Watching] = []
        for other in [*self.watching.values(), *self.finished]:
            if other is not mine and other.root == mine.root and other.begun <= time.time() and (other.ended is None or other.ended >= mine.begun):
                found.append(other)
        return found

    def stop(self, folder: Path, data: HookInput, found: Places | None) -> TraceData | None:
        ident = data.get("tool_use_id", "")
        with self.lock:
            mine = self.watching.pop(ident, None)
        if mine is None:
            return None
        else:
            touched = mine.watcher.finish()
            now = time.time()
            with self.lock:
                mine.ended = now
                mine.changed = touched.changed
                others: set[str] = set()
                for other in self.overlapping(mine):
                    others.update(other.watcher.seen())
                    others.update(other.changed)
                kept: list[Watching] = [mine]
                for each in self.finished:
                    if each.ended is not None and now - each.ended < LINGER:
                        kept.append(each)
                self.finished = kept
            edited: set[str] = set()
            if found is not None:
                for entry in recent(found):
                    if entry["session"] != mine.session and mine.begun <= entry["time"] <= now:
                        edited.add(entry["path"])
            traced = summary(mine.root, touched, others, edited)
            traced["seconds"] = data.get("duration_ms", 0) / 1000
            write_trace(folder, ident, traced)
            return traced

    def sweep(self) -> None:
        now = time.time()
        gone: list[Watching] = []
        with self.lock:
            for ident, each in list(self.watching.items()):
                if each.until < now:
                    gone.append(self.watching.pop(ident))
        for each in gone:
            each.watcher.finish()


def pre_tool(ctx: Context, settings: Settings, tracer: Threaded) -> str | None:
    tool = ctx.data.get("tool_name", "")
    raw = edited_path(ctx.data)
    if tool == "Bash":
        tracer.start(ctx.repo.root, ctx.session, ctx.data)
        return None
    elif raw is not None and tool in EDITS:
        path = relative(ctx.repo, raw)
        if path is None:
            return None
        else:
            return told("PreToolUse", outside(ctx, path) + elsewhere(ctx, path, settings.edit_window) + file_leads(ctx, settings, path))
    else:
        return None


def post_tool(ctx: Context, settings: Settings, tracer: Threaded) -> str | None:
    tool = ctx.data.get("tool_name", "")
    event = ctx.data.get("hook_event_name", "")
    raw = edited_path(ctx.data)
    if tool == "Bash":
        trace = tracer.stop(ctx.home / TRACES, ctx.data, ctx.places)
        if trace is None:
            return None
        else:
            paths: list[str] = []
            for item in trace["changed"]:
                saw(ctx, item["path"])
                paths.append(item["path"])
            if len(paths) == 0 or event != "PostToolUse":
                return None
            else:
                return answered(event, message("hook_changed", files=shortened(paths)))
    elif raw is None or event != "PostToolUse":
        return None
    else:
        path = relative(ctx.repo, raw)
        if path is None:
            return None
        elif tool == "Read":
            saw(ctx, path)
            return told(event, file_leads(ctx, settings, path))
        elif tool in EDITS:
            saw(ctx, path)
            note_edit(ctx, path, settings.edit_window)
            return None
        else:
            return None


def shortened(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:CHANGED_SHOWN])
    if len(paths) > CHANGED_SHOWN:
        return f"{shown} and {len(paths) - CHANGED_SHOWN} more"
    else:
        return shown


def text_file(target: Path) -> bytes | None:
    if target.is_file():
        data = target.read_bytes()
        if b"\x00" in data:
            return None
        else:
            return data
    else:
        return None


def ignored_by_git(root: Path, paths: Sequence[str]) -> set[str]:
    if len(paths) == 0 or not (root / ".git").exists():
        return set()
    else:
        done = subprocess.run(["git", "check-ignore", "--stdin"], cwd=root, input="\n".join(paths) + "\n", capture_output=True, text=True, errors="replace")
        return set(done.stdout.splitlines())


def summary(root: Path, touched: Touched, others: set[str], edited: set[str]) -> TraceData:
    read: list[SeenTrace] = []
    for path in sorted(touched.opened - touched.changed):
        digest: str | None = None
        if (root / path).is_file():
            digest = hashed((root / path).read_bytes())
        read.append({"path": path, "digest": digest})
    ignored = ignored_by_git(root, sorted(touched.changed))
    changed: list[ChangeTrace] = []
    shared: list[str] = []
    for path in sorted(touched.changed):
        if path in ignored or path in edited:
            pass
        elif path in others:
            shared.append(path)
        elif not (root / path).exists():
            changed.append({"path": path, "content": None})
        else:
            data = text_file(root / path)
            if data is not None:
                changed.append({"path": path, "content": data.decode(errors="replace")})
    return {"seconds": 0.0, "read": read, "changed": changed, "complete": touched.complete, "shared": shared}


def logged(line: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}", file=sys.stderr, flush=True)


def work(settings: Settings, data: HookInput) -> int:
    path = Path(data.get("transcript_path", ""))
    if path.is_file():
        return worked(settings, context(data), data.get("hook_event_name", ""), path)
    else:
        return 0


def worked(settings: Settings, ctx: Context, event: str, path: Path) -> int:
    ending = event == "SessionEnd"
    failed = 0
    with locked(ctx.home / WORK):
        ingest(ctx.places.home, ctx.session, path, ctx.repo, ending, ctx.home / TRACES)
        if event != "SubagentStop":
            extractor = Extractor(ctx.places, settings, Usage())
            done = extract_session(ctx.places, ctx.store(), ctx.session, extractor, ending, None, threading.Event())
            for reason in done.dropped:
                logged(reason)
            for reason in done.failed:
                logged(f"extraction failed: {reason}")
            failed = len(done.failed)
    if failed > 0:
        return 1
    else:
        return 0


def letters(ctx: Context, starting: bool) -> list[str]:
    office = Office(ctx.places.home)
    address = address_of(ctx.repo.name, ctx.session)
    found: list[str] = []
    if starting:
        me = office.register(address)
        if isinstance(me, Address):
            found.append(message("post_session", number=str(me.number), address=address))
    for text in delivered(office, address):
        found.append(message("post_letter_session", address=address, letter=text))
    return found


def with_letters(event: str, said: str | None, extra: list[str]) -> str | None:
    if len(extra) == 0:
        return said
    elif said is None:
        return answered(event, "\n\n".join(extra))
    else:
        shown: dict[str, dict[str, str]] = json.loads(said)
        context_text = shown["hookSpecificOutput"]["additionalContext"]
        return answered(event, "\n\n".join([*extra, context_text]))


def followed(cwd: Path) -> bool:
    return LAIN not in cwd.parts and in_checkout(cwd)


def handle(settings: Settings, data: HookInput, tracer: Threaded, later: Callable[[HookInput], None]) -> str | None:
    ctx = context(data)
    match data.get("hook_event_name"):
        case "SessionStart":
            pid = data.get("claude_pid")
            if pid is not None:
                began(ctx.home, pid)
            return with_letters("SessionStart", session_start(ctx, settings), letters(ctx, True))
        case "PreToolUse":
            return with_letters("PreToolUse", pre_tool(ctx, settings, tracer), letters(ctx, False))
        case "PostToolUse":
            return with_letters("PostToolUse", post_tool(ctx, settings, tracer), letters(ctx, False))
        case "PostToolUseFailure":
            return post_tool(ctx, settings, tracer)
        case event if event in HEAVY:
            if event == "SessionEnd":
                ended(ctx.home)
            later(data)
            return None
        case _:
            return None


def http_hook(port: int) -> dict[str, JSON]:
    return {"type": "http", "url": f"{address(port)}/hook"}


def start_hook() -> dict[str, JSON]:
    return {"type": "command", "command": sys.executable, "args": [str(LAUNCHER), "hook"]}


def settings_hooks(port: int) -> dict[str, JSON]:
    found: dict[str, JSON] = {
        "SessionStart": [{"hooks": [start_hook()]}],
        "UserPromptSubmit": [{"hooks": [http_hook(port)]}],
        "PreToolUse": [{"matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash", "hooks": [http_hook(port)]}],
        "PostToolUse": [{"hooks": [http_hook(port)]}],
        "PostToolUseFailure": [{"matcher": "Bash", "hooks": [http_hook(port)]}],
    }
    for event in HEAVY:
        found[event] = [{"hooks": [http_hook(port)]}]
    return found


def settings_of(path: Path) -> dict[str, JSON]:
    if path.is_file():
        found: dict[str, JSON] = json.loads(path.read_text())
        return found
    else:
        return {}


def groups_of(hooks: dict[str, JSON]) -> list[JSON]:
    found: list[JSON] = []
    for groups in hooks.values():
        if isinstance(groups, list):
            found.extend(groups)
    return found


def handlers_of(group: JSON) -> list[dict[str, JSON]]:
    found: list[dict[str, JSON]] = []
    if isinstance(group, dict):
        handlers = group.get("hooks")
        if isinstance(handlers, list):
            for handler in handlers:
                if isinstance(handler, dict):
                    found.append(handler)
    return found


def handled_by(group: JSON, wanted: Callable[[dict[str, JSON]], bool]) -> bool:
    for handler in handlers_of(group):
        if wanted(handler):
            return True
    return False


def ours(handler: dict[str, JSON], port: int) -> bool:
    return handler.get("args") == [str(LAUNCHER), "hook"] or handler == http_hook(port)


def handlers_in(path: Path, port: int) -> list[dict[str, JSON]]:
    found: list[dict[str, JSON]] = []
    hooks = settings_of(path).get("hooks")
    if isinstance(hooks, dict):
        for group in groups_of(hooks):
            for handler in handlers_of(group):
                if ours(handler, port):
                    found.append(handler)
    return found


def without(hooks: dict[str, JSON], wanted: Callable[[dict[str, JSON]], bool]) -> None:
    for event in list(hooks):
        groups = hooks[event]
        if isinstance(groups, list):
            kept: list[JSON] = []
            for group in groups:
                if not handled_by(group, wanted):
                    kept.append(group)
            if len(kept) == 0:
                del hooks[event]
            else:
                hooks[event] = kept


def install(path: Path, adding: bool, port: int) -> str:
    settings = settings_of(path)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return f"{path} has hooks that are not a JSON object; left it alone"
    else:
        without(hooks, lambda handler: ours(handler, port))
        if adding:
            for event, groups in settings_hooks(port).items():
                existing = hooks.get(event, [])
                if isinstance(existing, list) and isinstance(groups, list):
                    hooks[event] = existing + groups
        settings["hooks"] = hooks
        path.parent.mkdir(parents=True, exist_ok=True)
        replace_text(path, json.dumps(settings, indent=2) + "\n")
        if adding:
            return f"installed the hooks in {path}"
        else:
            return f"removed the hooks from {path}"
