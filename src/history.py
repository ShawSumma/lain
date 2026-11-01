from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from page import Builder, Page, Unreadable, flag, flagged, number, pages, read, real, rendered, when, write
from store import JSON, claimed
from tree import Change, Seen, SeenKind


@dataclass(frozen=True)
class Call:
    id: str
    tool: str
    arguments: str

    def given(self) -> dict[str, JSON]:
        try:
            value: JSON = json.loads(self.arguments)
        except json.JSONDecodeError:
            return {}
        else:
            if isinstance(value, dict):
                return value
            else:
                return {}


@dataclass(frozen=True)
class Tokens:
    prompt: int
    cached: int
    completion: int


@dataclass(frozen=True)
class Request:
    text: str


@dataclass(frozen=True)
class Brief:
    text: str
    origin: str
    system: str
    tools: str


@dataclass(frozen=True)
class Reply:
    thinking: str
    content: str
    calls: tuple[Call, ...]
    finish: str | None
    tokens: Tokens | None
    seconds: float


@dataclass(frozen=True)
class Result:
    call: str
    tool: str
    text: str
    ok: bool
    patch: tuple[Change, ...]
    reads: tuple[Seen, ...] | None
    seconds: float


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class Dispatch:
    agent: str
    role: str
    task: str


@dataclass(frozen=True)
class Planned:
    agent: str
    arguments: str


@dataclass(frozen=True)
class Reported:
    agent: str
    arguments: str


@dataclass(frozen=True)
class GaveUp:
    agent: str
    reason: str


@dataclass(frozen=True)
class Reviewed:
    agent: str
    arguments: str


@dataclass(frozen=True)
class Unreviewed:
    agent: str


@dataclass(frozen=True)
class Accepted:
    agent: str
    summary: str
    patch: tuple[Change, ...]
    base: tuple[Seen, ...]


@dataclass(frozen=True)
class Rejected:
    reason: str


@dataclass(frozen=True)
class Finished:
    goal: str


@dataclass(frozen=True)
class Applied:
    paths: tuple[str, ...]


type Body = (
    Request
    | Brief
    | Reply
    | Result
    | Note
    | Dispatch
    | Planned
    | Reported
    | GaveUp
    | Reviewed
    | Unreviewed
    | Accepted
    | Rejected
    | Finished
    | Applied
)


@dataclass(frozen=True)
class Step:
    number: int
    parent: int | None
    body: Body
    time: float


type Kind = Literal[
    "request",
    "brief",
    "reply",
    "result",
    "note",
    "dispatch",
    "planned",
    "reported",
    "gave_up",
    "reviewed",
    "unreviewed",
    "accepted",
    "rejected",
    "finished",
    "applied",
]


def file_name(number: int) -> str:
    return f"{number}.md"


def put_changes(built: Builder, key: str, changes: Sequence[Change]) -> None:
    for change in changes:
        built.field(key, change.path)
        if change.content is not None:
            built.section(f"{key} {change.path}", change.content)


def changes_of(page: Page, key: str) -> tuple[Change, ...]:
    found: list[Change] = []
    for path in page.every(key):
        found.append(Change(path, page.text(f"{key} {path}")))
    return tuple(found)


def put_seen(built: Builder, key: str, seen: Sequence[Seen]) -> None:
    for item in seen:
        built.field(key, f"{item.kind} {item.digest or '-'} {item.path}")


def seen_kind(text: str) -> SeenKind:
    match text:
        case "file" | "listing" | "tree" | "bound":
            return text
        case _:
            raise Unreadable(f"'{text}' is not a kind of reading")


def seen_of(page: Page, key: str) -> tuple[Seen, ...]:
    found: list[Seen] = []
    for line in page.every(key):
        kind, _, rest = line.partition(" ")
        digest, _, path = rest.partition(" ")
        if digest == "-":
            found.append(Seen(path, seen_kind(kind), None))
        else:
            found.append(Seen(path, seen_kind(kind), digest))
    return tuple(found)


def put_calls(built: Builder, calls: Sequence[Call]) -> None:
    for call in calls:
        built.field("call", f"{call.id} {call.tool}")
        built.section(f"arguments {call.id}", call.arguments)


def calls_of(page: Page) -> tuple[Call, ...]:
    found: list[Call] = []
    for line in page.every("call"):
        ident, _, tool = line.partition(" ")
        found.append(Call(ident, tool, page.text(f"arguments {ident}") or ""))
    return tuple(found)


def tokens_text(tokens: Tokens | None) -> str:
    if tokens is None:
        return "none"
    else:
        return f"{tokens.prompt} in, {tokens.cached} cached, {tokens.completion} out"


def tokens_of(text: str) -> Tokens | None:
    if text == "none":
        return None
    else:
        parts = text.replace(",", "").split()
        return Tokens(number(parts[0]), number(parts[2]), number(parts[4]))


def optional(text: str | None) -> str:
    if text is None:
        return "none"
    else:
        return text


def numbered_or_none(value: int | None) -> str:
    if value is None:
        return "none"
    else:
        return str(value)


def kind_of(body: Body) -> Kind:
    match body:
        case Request():
            return "request"
        case Brief():
            return "brief"
        case Reply():
            return "reply"
        case Result():
            return "result"
        case Note():
            return "note"
        case Dispatch():
            return "dispatch"
        case Planned():
            return "planned"
        case Reported():
            return "reported"
        case GaveUp():
            return "gave_up"
        case Reviewed():
            return "reviewed"
        case Unreviewed():
            return "unreviewed"
        case Accepted():
            return "accepted"
        case Rejected():
            return "rejected"
        case Finished():
            return "finished"
        case Applied():
            return "applied"


def heading(step: Step) -> str:
    match step.body:
        case Result(tool=tool, ok=True):
            return f"step {step.number}: {tool} answered"
        case Result(tool=tool):
            return f"step {step.number}: {tool} failed"
        case Reply(calls=calls) if len(calls) > 0:
            names: list[str] = []
            for call in calls:
                names.append(call.tool)
            return f"step {step.number}: reply calling {', '.join(names)}"
        case _:
            return f"step {step.number}: {kind_of(step.body)}"


def encode(step: Step) -> Page:
    built = Builder(heading(step))
    built.field("kind", kind_of(step.body))
    built.field("parent", numbered_or_none(step.parent))
    built.field("time", when(step.time))
    match step.body:
        case Request(text) | Note(text):
            built.section("text", text)
        case Brief(text, origin, system, tools):
            built.field("origin", origin)
            built.field("tools", tools)
            built.section("text", text)
            built.section("system", system)
        case Reply(thinking, content, calls, finish, tokens, seconds):
            built.field("finish", optional(finish))
            built.field("tokens", tokens_text(tokens))
            built.field("seconds", repr(seconds))
            put_calls(built, calls)
            built.section("thinking", thinking)
            built.section("content", content)
        case Result(call, tool, text, ok, patch, reads, seconds):
            built.field("answers", call)
            built.field("tool", tool)
            built.field("ok", flag(ok))
            built.field("seconds", repr(seconds))
            if reads is None:
                built.field("reads", "untraced")
            else:
                built.field("reads", "traced")
                put_seen(built, "read", reads)
            put_changes(built, "change", patch)
            built.section("text", text)
        case Dispatch(agent, role, task):
            built.field("agent", agent)
            built.field("role", role)
            built.section("task", task)
        case Planned(agent, arguments) | Reported(agent, arguments) | Reviewed(agent, arguments):
            built.field("agent", agent)
            built.section("arguments", arguments)
        case GaveUp(agent, reason):
            built.field("agent", agent)
            built.section("reason", reason)
        case Unreviewed(agent):
            built.field("agent", agent)
        case Accepted(agent, summary, patch, base):
            built.field("agent", agent)
            put_seen(built, "base", base)
            put_changes(built, "change", patch)
            built.section("summary", summary)
        case Rejected(reason):
            built.section("reason", reason)
        case Finished(goal):
            built.section("goal", goal)
        case Applied(paths):
            for path in paths:
                built.field("path", path)
    return built.page()


def reads_of(page: Page) -> tuple[Seen, ...] | None:
    if page.need("reads") == "untraced":
        return None
    else:
        return seen_of(page, "read")


def finish_of(text: str) -> str | None:
    if text == "none":
        return None
    else:
        return text


def body_of(page: Page) -> Body:
    match page.need("kind"):
        case "request":
            return Request(page.value("text"))
        case "brief":
            return Brief(page.value("text"), page.value("origin"), page.value("system"), page.value("tools"))
        case "reply":
            tokens = tokens_of(page.need("tokens"))
            return Reply(page.value("thinking"), page.value("content"), calls_of(page), finish_of(page.need("finish")), tokens, real(page.need("seconds")))
        case "result":
            ok = flagged(page.need("ok"))
            return Result(page.value("answers"), page.value("tool"), page.value("text"), ok, changes_of(page, "change"), reads_of(page), real(page.need("seconds")))
        case "note":
            return Note(page.value("text"))
        case "dispatch":
            return Dispatch(page.value("agent"), page.value("role"), page.value("task"))
        case "planned":
            return Planned(page.value("agent"), page.value("arguments"))
        case "reported":
            return Reported(page.value("agent"), page.value("arguments"))
        case "gave_up":
            return GaveUp(page.value("agent"), page.value("reason"))
        case "reviewed":
            return Reviewed(page.value("agent"), page.value("arguments"))
        case "unreviewed":
            return Unreviewed(page.value("agent"))
        case "accepted":
            return Accepted(page.value("agent"), page.value("summary"), changes_of(page, "change"), seen_of(page, "base"))
        case "rejected":
            return Rejected(page.value("reason"))
        case "finished":
            return Finished(page.value("goal"))
        case "applied":
            return Applied(tuple(page.every("path")))
        case other:
            raise Unreadable(f"'{other}' is not a kind of step")


def parent_of(text: str) -> int | None:
    if text == "none":
        return None
    else:
        return number(text)


def decode(path: Path) -> Step:
    page = read(path)
    return Step(number(path.stem), parent_of(page.need("parent")), body_of(page), real(page.need("time")))


class History:
    home: Path
    directory: Path
    head_path: Path
    steps: dict[int, Step]
    children: dict[int | None, list[int]]
    head: int | None
    top: int

    def __init__(self, home: Path) -> None:
        self.home = home
        self.directory = home / "steps"
        self.head_path = home / "head.md"
        self.steps = {}
        self.children = {}
        self.top = 0
        for path in pages(self.directory):
            step = decode(path)
            self.steps[step.number] = step
            self.top = max(self.top, step.number)
        for each in sorted(self.steps):
            self.children.setdefault(self.steps[each].parent, []).append(each)
        self.head = None
        if self.head_path.is_file():
            self.head = parent_of(read(self.head_path).need("step"))

    def move(self, step: int | None) -> None:
        self.head = step
        write(self.head_path, Page(f"head of {self.home.name}", (("step", numbered_or_none(step)),), ()))

    def follow(self, body: Body) -> int | None:
        for number in self.children.get(self.head, []):
            if self.steps[number].body == body:
                self.move(number)
                return number
        return None

    def follow_reply(self) -> Reply | None:
        for number in self.children.get(self.head, []):
            match self.steps[number].body:
                case Reply() as reply:
                    self.move(number)
                    return reply
                case _:
                    pass
        return None

    def answers_to(self, call: str) -> list[Step]:
        found: list[Step] = []
        for number in self.children.get(self.head, []):
            match self.steps[number].body:
                case Result(call=answered) if answered == call:
                    found.append(self.steps[number])
                case _:
                    pass
        return found

    def append(self, body: Body, stamp: float | None = None) -> Step:
        self.top += 1
        if stamp is None:
            stamp = time.time()
        step = Step(self.top, self.head, body, stamp)
        if claimed(self.directory / file_name(step.number), rendered(encode(step)).encode()):
            self.steps[step.number] = step
            self.children.setdefault(step.parent, []).append(step.number)
            self.move(step.number)
            return step
        else:
            return self.append(body, stamp)

    def path_to(self, number: int | None) -> list[Step]:
        steps: list[Step] = []
        current = number
        while current is not None:
            step = self.steps[current]
            steps.append(step)
            current = step.parent
        steps.reverse()
        return steps

    def lineage(self) -> list[Step]:
        return self.path_to(self.head)

    def ancestors(self) -> set[int]:
        found: set[int] = set()
        for step in self.lineage():
            found.add(step.number)
        return found
