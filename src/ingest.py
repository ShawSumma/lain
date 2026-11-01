from __future__ import annotations

import json
import os
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TypedDict, cast

from history import Brief, Call, Dispatch, History, Note, Reply, Request, Result, Tokens
from page import Builder, Page, Unreadable, flag, flagged, fresh_dir, number, pages, read, real, when, write
from places import LAIN, REVIEWS, RUNS, SESSIONS, Places, Repo, repo_of
from store import JSON, locked, record_of
from transcript import Block, Record, UsageData, blocks, load
from tree import Change, Seen, hashed

CLAUDE = Path.home() / ".claude"
STATE = "state.md"
TRACES = "traces"
LIVE = "live.md"
INDEX = "index.md"
SUBAGENTS = "subagents"


class StateData(TypedDict):
    source: str
    steps: dict[str, int | None]
    children: dict[str, int]
    extracted: list[int]


class SeenTrace(TypedDict):
    path: str
    digest: str | None


class ChangeTrace(TypedDict):
    path: str
    content: str | None


class TraceData(TypedDict):
    seconds: float
    read: list[SeenTrace]
    changed: list[ChangeTrace]
    complete: bool
    shared: list[str]


@dataclass
class Group:
    records: list[Record]


def session_name(found: Places, session: str, stamp: float) -> str:
    folder = found.home / SESSIONS
    with locked(folder):
        index = folder / INDEX
        known: list[tuple[str, str]] = []
        if index.is_file():
            for line in read(index).every("session"):
                ident, _, name = line.partition(" ")
                known.append((ident, name))
                if ident == session:
                    return f"{SESSIONS}/{name}"
        made = fresh_dir(folder, stamp).name
        known.append((session, made))
        built = Builder("Claude Code sessions and the folders they are kept in")
        for ident, name in known:
            built.field("session", f"{ident} {name}")
        write(index, built.page())
        return f"{SESSIONS}/{made}"


def agent_names(found: Places) -> list[str]:
    names: list[str] = []
    if (found.agent(REVIEWS) / "steps").is_dir():
        names.append(REVIEWS)
    names.extend(session_homes(found))
    if (found.home / RUNS).is_dir():
        for home in sorted((found.home / RUNS).iterdir()):
            if (home / "steps").is_dir():
                names.append(f"{RUNS}/{home.name}")
    return names


def session_homes(found: Places) -> list[str]:
    names: list[str] = []
    folder = found.home / SESSIONS
    if folder.is_dir():
        for home in sorted(folder.iterdir()):
            if (home / STATE).is_file():
                names.append(f"{SESSIONS}/{home.name}")
    return names


def relative(repo: Repo, raw: str) -> str | None:
    path = Path(raw)
    if not path.is_absolute():
        return Path(os.path.normpath(raw)).as_posix()
    elif path.is_relative_to(repo.root):
        return path.relative_to(repo.root).as_posix()
    else:
        return None


def stamp_of(record: Record) -> float:
    return datetime.fromisoformat(record["timestamp"]).timestamp()


def flatten(content: str | list[Block]) -> str:
    if isinstance(content, str):
        return content
    else:
        parts: list[str] = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)


def text_of(record: Record) -> str | None:
    message = record.get("message")
    if message is None:
        return None
    else:
        parts: list[str] = []
        for block in blocks(message):
            if block.get("type") == "tool_result":
                return None
            elif block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)


def tokens_of(usage: UsageData | None) -> Tokens | None:
    if usage is None:
        return None
    else:
        cached = usage.get("cache_read_input_tokens", 0)
        prompt = usage.get("input_tokens", 0) + cached + usage.get("cache_creation_input_tokens", 0)
        return Tokens(prompt, cached, usage.get("output_tokens", 0))


def reply_of(group: Group) -> Reply:
    thinking: list[str] = []
    content: list[str] = []
    calls: list[Call] = []
    finish: str | None = None
    usage: UsageData | None = None
    for record in group.records:
        message = record.get("message", {})
        finish = message.get("stop_reason", finish)
        usage = message.get("usage", usage)
        for block in blocks(message):
            match block.get("type"):
                case "thinking":
                    thinking.append(block.get("thinking", ""))
                case "text":
                    content.append(block.get("text", ""))
                case "tool_use":
                    calls.append(Call(block.get("id", ""), block.get("name", ""), json.dumps(block.get("input", {}))))
                case _:
                    pass
    return Reply("\n".join(thinking), "\n".join(content), tuple(calls), finish, tokens_of(usage), 0.0)


def edited(original: str, old: str, new: str, every: bool) -> str:
    if every:
        return original.replace(old, new)
    else:
        return original.replace(old, new, 1)


def string(data: dict[str, JSON], key: str) -> str | None:
    value = data.get(key)
    if isinstance(value, str):
        return value
    else:
        return None


def path_in(repo: Repo, data: dict[str, JSON]) -> str | None:
    raw = string(data, "filePath")
    if raw is None:
        return None
    else:
        return relative(repo, raw)


def traced_patch(trace: TraceData) -> tuple[Change, ...]:
    found: list[Change] = []
    for item in trace["changed"]:
        found.append(Change(item["path"], item["content"]))
    return tuple(found)


def tool_patch(tool: str, path: str, result: dict[str, JSON]) -> tuple[Change, ...]:
    match tool:
        case "Write":
            content = string(result, "content")
            if content is None:
                return ()
            else:
                return (Change(path, content),)
        case "Edit":
            original = string(result, "originalFile")
            old = string(result, "oldString")
            new = string(result, "newString")
            if original is None or old is None or new is None:
                return ()
            else:
                return (Change(path, edited(original, old, new, result.get("replaceAll") is True)),)
        case _:
            return ()


def patch_of(repo: Repo, tool: str, result: JSON, trace: TraceData | None) -> tuple[Change, ...]:
    if trace is not None:
        return traced_patch(trace)
    elif not isinstance(result, dict):
        return ()
    else:
        path = path_in(repo, result)
        if path is None:
            return ()
        else:
            return tool_patch(tool, path, result)


def traced_reads(trace: TraceData) -> tuple[Seen, ...] | None:
    if not trace["complete"]:
        return None
    else:
        found: list[Seen] = []
        for item in trace["read"]:
            found.append(Seen(item["path"], "file", item["digest"]))
        return tuple(found)


def file_read(repo: Repo, shown: dict[str, JSON]) -> tuple[Seen, ...]:
    path = path_in(repo, shown)
    if path is None:
        return ()
    else:
        content = string(shown, "content")
        whole = shown.get("startLine") == 1 and shown.get("numLines") == shown.get("totalLines")
        digest: str | None = None
        if content is not None and whole:
            digest = hashed(content.encode())
        return (Seen(path, "file", digest),)


def reads_of(repo: Repo, tool: str, result: JSON, trace: TraceData | None) -> tuple[Seen, ...] | None:
    if trace is not None:
        return traced_reads(trace)
    elif tool == "Bash":
        return None
    elif tool != "Read" or not isinstance(result, dict):
        return ()
    else:
        shown = result.get("file")
        if isinstance(shown, dict):
            return file_read(repo, shown)
        else:
            return ()


def groups(records: Sequence[Record]) -> list[Record | Group]:
    found: list[Record | Group] = []
    for record in records:
        if record.get("type") == "assistant" and "uuid" in record:
            identity = record.get("message", {}).get("id")
            if len(found) > 0 and isinstance(found[-1], Group) and found[-1].records[0].get("message", {}).get("id") == identity:
                found[-1].records.append(record)
            else:
                found.append(Group([record]))
        else:
            found.append(record)
    return found


def trailing(items: list[Record | Group]) -> Group | None:
    for item in reversed(items):
        if isinstance(item, Group):
            return item
        elif item.get("type") in ("user", "system"):
            return None
    return None


def empty_state(source: Path) -> StateData:
    return {"source": str(source), "steps": {}, "children": {}, "extracted": []}


def step_or_none(text: str) -> int | None:
    if text == "none":
        return None
    else:
        return number(text)


def tail(state: StateData) -> int | None:
    found: int | None = None
    for step in state["steps"].values():
        if step is not None and (found is None or step > found):
            found = step
    return found


def read_state(home: Path) -> StateData | None:
    if (home / STATE).is_file():
        page = read(home / STATE)
        state = empty_state(Path(page.need("source")))
        for line in page.every("record"):
            ident, _, step = line.rpartition(" ")
            state["steps"][ident] = step_or_none(step)
        for line in page.every("child"):
            ident, _, step = line.rpartition(" ")
            state["children"][ident] = number(step)
        for line in page.every("extracted"):
            state["extracted"].append(number(line))
        return state
    else:
        return None


def write_state(home: Path, state: StateData) -> None:
    built = Builder(f"what {home.name} has read in and extracted")
    built.field("source", state["source"])
    for ident, step in state["steps"].items():
        if step is None:
            built.field("record", f"{ident} none")
        else:
            built.field("record", f"{ident} {step}")
    for ident, step in state["children"].items():
        built.field("child", f"{ident} {step}")
    for step in state["extracted"]:
        built.field("extracted", str(step))
    write(home / STATE, built.page())


def began(home: Path, pid: int) -> None:
    write(home / LIVE, Page("the Claude Code process running this session", (("process", str(pid)),), ()))


def ended(home: Path) -> None:
    (home / LIVE).unlink(missing_ok=True)


def live_session(home: Path) -> bool:
    if not (home / LIVE).is_file():
        return False
    else:
        return alive(number(read(home / LIVE).need("process")))


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    else:
        return True


def live(history: History) -> set[int]:
    found: set[int] = set()
    for step in history.lineage():
        found.add(step.number)
    return found


def trace_page(ident: str, trace: TraceData) -> Page:
    built = Builder(f"what the command {ident} read and changed")
    built.field("tool use", ident)
    built.field("seconds", repr(trace["seconds"]))
    built.field("complete", flag(trace["complete"]))
    for item in trace["read"]:
        built.field("read", f"{item['digest'] or '-'} {item['path']}")
    for path in trace["shared"]:
        built.field("shared", path)
    for change in trace["changed"]:
        built.field("changed", change["path"])
        if change["content"] is not None:
            built.section(f"content {change['path']}", change["content"])
    return built.page()


def write_trace(folder: Path, ident: str, trace: TraceData) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    with locked(folder):
        write(folder / f"{len(pages(folder)) + 1}.md", trace_page(ident, trace))


def trace_of(page: Page) -> TraceData:
    reads: list[SeenTrace] = []
    for line in page.every("read"):
        digest, _, path = line.partition(" ")
        if digest == "-":
            reads.append({"path": path, "digest": None})
        else:
            reads.append({"path": path, "digest": digest})
    changed: list[ChangeTrace] = []
    for path in page.every("changed"):
        changed.append({"path": path, "content": page.text(f"content {path}")})
    return {"seconds": real(page.need("seconds")), "read": reads, "changed": changed, "complete": flagged(page.need("complete")), "shared": page.every("shared")}


def read_traces(home: Path) -> dict[str, TraceData]:
    found: dict[str, TraceData] = {}
    for path in pages(home / TRACES):
        page = read(path)
        found[page.need("tool use")] = trace_of(page)
    return found


@dataclass(frozen=True)
class Parent:
    number: int | None


class Mapper:
    name: str
    repo: Repo
    history: History
    state: StateData
    traces: dict[str, TraceData]
    brief: str | None
    calls: dict[str, Call]
    added: int
    last: int | None
    previous: int | None

    def __init__(self, name: str, repo: Repo, history: History, state: StateData, traces: dict[str, TraceData], brief: str | None) -> None:
        self.name = name
        self.repo = repo
        self.history = history
        self.state = state
        self.traces = traces
        self.brief = brief
        self.calls = {}
        for step in history.steps.values():
            match step.body:
                case Reply(calls=calls):
                    for call in calls:
                        self.calls[call.id] = call
                case _:
                    pass
        self.added = 0
        self.last = None
        self.previous = None

    def parent(self, record: Record) -> Parent | None:
        raw = record.get("parentUuid")
        logical = record.get("logicalParentUuid")
        if raw is not None and raw in self.state["steps"]:
            return Parent(self.state["steps"][raw])
        elif raw is not None:
            return None
        elif logical is not None and logical in self.state["steps"]:
            return Parent(self.state["steps"][logical])
        elif logical is not None:
            return Parent(self.previous)
        else:
            return Parent(None)

    def append(self, parent: int | None, made: Reply | Request | Result | Note | Brief | Dispatch, when: float) -> int:
        self.history.move(parent)
        step = self.history.append(made, when)
        self.added += 1
        return step.number

    def take(self, item: Record | Group, held: Group | None) -> None:
        if isinstance(item, Group):
            first = item.records[0]
        else:
            first = item
        uuid = first.get("uuid")
        if uuid is not None and uuid not in self.state["steps"] and item is not held:
            parent = self.parent(first)
            if parent is not None:
                self.place(item, first, uuid, parent)
        if uuid is not None and uuid in self.state["steps"]:
            self.previous = self.state["steps"][uuid]

    def place(self, item: Record | Group, first: Record, uuid: str, parent: Parent) -> None:
        number = self.step(item, first, parent.number)
        mapped = number
        if number is None:
            mapped = parent.number
        else:
            self.last = number
        if isinstance(item, Group):
            for record in item.records:
                self.state["steps"][record.get("uuid", "")] = mapped
        else:
            self.state["steps"][uuid] = mapped

    def step(self, item: Record | Group, record: Record, parent: int | None) -> int | None:
        when = stamp_of(record)
        if isinstance(item, Group):
            reply = reply_of(item)
            for call in reply.calls:
                self.calls[call.id] = call
            return self.append(parent, reply, when)
        else:
            match record.get("type"):
                case "user":
                    return self.user(record, parent, when)
                case "system" if record.get("subtype") == "compact_boundary":
                    return self.append(parent, Note("the context was compacted"), when)
                case _:
                    return None

    def answered(self, parent: int | None) -> int | None:
        if parent is None:
            return None
        else:
            match self.history.steps[parent].body:
                case Reply(calls=calls):
                    asked: set[str] = set()
                    for call in calls:
                        asked.add(call.id)
                    return self.last_answer(parent, asked)
                case _:
                    return parent

    def last_answer(self, parent: int, asked: set[str]) -> int:
        current = parent
        while True:
            later: int | None = None
            for child in self.history.children.get(current, []):
                match self.history.steps[child].body:
                    case Result(call=call) if call in asked:
                        later = child
                    case Dispatch():
                        later = child
                    case _:
                        pass
            if later is None:
                return current
            else:
                current = later

    def user(self, record: Record, parent: int | None, when: float) -> int | None:
        message = record.get("message")
        if message is None:
            return None
        else:
            results: list[Block] = []
            for block in blocks(message):
                if block.get("type") == "tool_result":
                    results.append(block)
            if len(results) > 0:
                number = self.answered(parent)
                for block in results:
                    number = self.result(record, block, number, when)
                return number
            else:
                return self.said(record, parent, when)

    def said(self, record: Record, parent: int | None, when: float) -> int | None:
        text = text_of(record)
        if text is None or record.get("isMeta") is True:
            return None
        elif record.get("isCompactSummary") is True:
            return self.append(parent, Note(text), when)
        elif self.brief is not None and parent is None:
            return self.append(parent, Brief(text, self.brief, "", "claude-code"), when)
        elif text.startswith("[Request interrupted") or record.get("origin", {}).get("kind") != "human":
            return self.append(parent, Note(text), when)
        else:
            return self.append(parent, Request(text), when)

    def result(self, record: Record, block: Block, parent: int | None, when: float) -> int:
        ident = block.get("tool_use_id", "")
        call = self.calls.get(ident)
        tool = "?"
        if call is not None:
            tool = call.tool
        outcome: JSON = record.get("toolUseResult")
        trace = self.traces.get(ident)
        text = flatten(block.get("content", ""))
        if call is not None and tool == "Agent" and isinstance(outcome, dict):
            agent = string(outcome, "agentId")
            if agent is not None:
                parent = self.dispatch(agent, call, outcome, parent, when)
        patch = patch_of(self.repo, tool, outcome, trace)
        reads = reads_of(self.repo, tool, outcome, trace)
        seconds = 0.0
        if trace is not None:
            seconds = trace["seconds"]
        made = Result(ident, tool, text, block.get("is_error") is not True, patch, reads, seconds)
        return self.append(parent, made, when)

    def dispatch(self, agent: str, call: Call, outcome: dict[str, JSON], parent: int | None, when: float) -> int:
        child = subagent(self.name, len(self.state["children"]) + 1)
        asked: dict[str, JSON] = json.loads(call.arguments)
        role = string(asked, "subagent_type")
        if role is None:
            role = "subagent"
        task = string(outcome, "prompt")
        if task is None:
            task = ""
        number = self.append(parent, Dispatch(child, role, task), when)
        self.state["children"][agent] = number
        return number


def subagent(name: str, order: int) -> str:
    return f"{name}/{SUBAGENTS}/{order}"


def subagents_of(path: Path, brief: str | None) -> Path:
    if brief is None:
        return path.parent / path.stem / "subagents"
    else:
        return path.parent


def ingest(agents: Path, name: str, path: Path, repo: Repo, final: bool, traces: Path, brief: str | None = None) -> int:
    home = agents / name
    with locked(home):
        transcript = load(path)
        history = History(home)
        state = read_state(home)
        if state is None:
            state = empty_state(path)
        mapper = Mapper(name, repo, history, state, read_traces(traces), brief)
        items = groups(transcript.records)
        held: Group | None = None
        if not final:
            held = trailing(items)
        for item in items:
            mapper.take(item, held)
        if mapper.last is not None:
            history.move(mapper.last)
        write_state(home, state)
        children = dict(state["children"])
    added = mapper.added
    for order, (agent, step) in enumerate(children.items(), 1):
        sub = subagents_of(path, brief) / f"agent-{agent}.jsonl"
        if sub.is_file():
            added += ingest(agents, subagent(name, order), sub, repo, final, traces, f"{name}/{step}")
    return added


def first_cwd(path: Path) -> Path | None:
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            data = record_of(line)
            if data is not None:
                cwd = data.get("cwd")
                if isinstance(cwd, str):
                    return Path(cwd)
    return None


def first_time(path: Path) -> float:
    with path.open(encoding="utf-8", errors="replace") as file:
        for line in file:
            data = record_of(line)
            if data is not None and isinstance(data.get("timestamp"), str):
                return stamp_of(cast(Record, data))
    return path.stat().st_mtime


def transcripts(project: Path) -> Iterator[Path]:
    main = repo_of(project).main
    for folder in sorted((CLAUDE / "projects").glob("*")):
        for path in sorted(folder.glob("*.jsonl")):
            cwd = first_cwd(path)
            if cwd is not None and LAIN not in cwd.parts and repo_of(cwd).main == main:
                yield path
