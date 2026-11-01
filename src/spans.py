from __future__ import annotations

import argparse
import os
import statistics
import sys
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, Required, TypedDict

from ingest import alive
from page import Builder, Page, Unreadable, number, read, real, when, write
from places import INSTALL, LAIN
from store import JSON

SPANS = INSTALL / "spans"
CUT = 4000
LARGEST = 8

type Kind = Literal["job", "commit", "stretch", "region", "recheck", "call", "tool", "session", "prompt", "work"]
type ChunkKind = Literal["text", "thinking", "input", "output", "note"]


class SpanRecord(TypedDict, total=False):
    id: Required[str]
    event: Required[Literal["start", "end"]]
    time: Required[float]
    pid: Required[int]
    parent: str | None
    kind: Kind
    name: str
    project: str
    stream: str
    attrs: dict[str, JSON]
    ok: bool


class ChunkData(TypedDict):
    span: str
    time: float
    kind: ChunkKind
    text: str


@dataclass
class Span:
    id: str
    stream: str
    project: str
    ok: bool = True
    attrs: dict[str, JSON] = field(default_factory=dict)


class Listener(Protocol):
    def heard_span(self, record: SpanRecord) -> None: ...

    def heard_chunk(self, piece: ChunkData) -> None: ...


current: ContextVar[str | None] = ContextVar("span", default=None)
lock = threading.Lock()
streams: dict[str, list[ChunkData]] = {}
starts: dict[str, SpanRecord] = {}
counters: dict[str, int] = {}
listeners: list[Listener] = []


def here() -> str | None:
    return current.get()


def listed(items: Iterable[str]) -> list[JSON]:
    found: list[JSON] = []
    for item in items:
        found.append(item)
    return found


def command_line() -> str:
    return " ".join([Path(sys.argv[0]).name, *sys.argv[1:]])[:CUT]


def cut(text: str) -> str:
    if len(text) > CUT:
        return f"{text[:CUT]}... ({len(text) - CUT} more characters)"
    else:
        return text


def path_of(ident: str) -> Path:
    return SPANS / f"{ident}.md"


def put_value(built: Builder, key: str, value: JSON) -> None:
    if isinstance(value, list):
        for item in value:
            put_value(built, key, item)
    elif isinstance(value, bool):
        built.field(key, str(value).lower())
    elif isinstance(value, int | float):
        built.field(key, repr(value))
    elif isinstance(value, str):
        built.field(key, value)
    elif value is not None:
        built.section(key, repr(value))


def value_of(text: str) -> JSON:
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def span_page(start: SpanRecord, end: SpanRecord | None, said: list[ChunkData]) -> Page:
    built = Builder(f"{start.get('kind', '')} {start.get('name', '')}")
    built.field("id", start["id"])
    built.field("kind", start.get("kind", ""))
    built.field("name", start.get("name", ""))
    built.field("project", start.get("project", ""))
    built.field("parent", start.get("parent") or "none")
    built.field("stream", start.get("stream", start["id"]))
    built.field("process", str(start["pid"]))
    built.field("started", when(start["time"]))
    for key, value in start.get("attrs", {}).items():
        put_value(built, f"given {key}", value)
    if end is not None:
        built.field("ended", when(end["time"]))
        built.field("ok", str(end.get("ok", False)).lower())
        for key, value in end.get("attrs", {}).items():
            put_value(built, f"came to {key}", value)
    for piece in said:
        built.section(f"{piece['kind']} from {piece['span']} at {piece['time']!r}", piece["text"])
    return built.page()


def attrs_of(page: Page, prefix: str) -> dict[str, JSON]:
    found: dict[str, JSON] = {}
    for key, text in page.fields:
        if key.startswith(prefix):
            name = key.removeprefix(prefix)
            value = value_of(text)
            if text in ("true", "false"):
                value = text == "true"
            held = found.get(name)
            if isinstance(held, list):
                held.append(value)
            elif name in found:
                found[name] = [held, value]
            else:
                found[name] = value
    return found


def chunk_kind(text: str) -> ChunkKind:
    match text:
        case "text" | "thinking" | "input" | "output" | "note":
            return text
        case _:
            raise Unreadable(f"'{text}' is not a kind of chunk")


def records_of(page: Page) -> tuple[SpanRecord, SpanRecord | None, list[ChunkData]]:
    ident = page.need("id")
    parent: str | None = page.need("parent")
    if parent == "none":
        parent = None
    start: SpanRecord = {
        "id": ident,
        "event": "start",
        "time": real(page.need("started")),
        "pid": number(page.need("process")),
        "parent": parent,
        "kind": kind_of(page.need("kind")),
        "name": page.need("name"),
        "project": page.need("project"),
        "stream": page.need("stream"),
        "attrs": attrs_of(page, "given "),
    }
    end: SpanRecord | None = None
    if page.one("ended") is not None:
        end = {"id": ident, "event": "end", "time": real(page.need("ended")), "pid": start["pid"], "ok": page.need("ok") == "true", "attrs": attrs_of(page, "came to ")}
    said: list[ChunkData] = []
    for heading, text in page.sections:
        kind, _, rest = heading.partition(" from ")
        owner, _, stamp = rest.partition(" at ")
        said.append({"span": owner, "time": real(stamp), "kind": chunk_kind(kind), "text": text})
    return start, end, said


def kind_of(text: str) -> Kind:
    match text:
        case "job" | "commit" | "stretch" | "region" | "recheck" | "call" | "tool" | "session" | "prompt" | "work":
            return text
        case _:
            raise Unreadable(f"'{text}' is not a kind of span")


def fresh(stamp: float) -> str:
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    with lock:
        if day not in counters:
            top = 0
            if (SPANS / day).is_dir():
                for path in (SPANS / day).glob("*.md"):
                    if path.stem.isdigit():
                        top = max(top, int(path.stem))
            counters[day] = top
        while True:
            counters[day] += 1
            ident = f"{day}/{counters[day]}"
            path_of(ident).parent.mkdir(parents=True, exist_ok=True)
            try:
                path_of(ident).open("x").close()
            except FileExistsError:
                pass
            else:
                return ident


def started(kind: Kind, name: str, project: str, parent: str | None, attrs: dict[str, JSON], stream: str | None, pid: int, stamp: float) -> Span:
    ident = fresh(stamp)
    record: SpanRecord = {
        "id": ident,
        "event": "start",
        "time": stamp,
        "pid": pid,
        "parent": parent,
        "kind": kind,
        "name": name,
        "project": project,
        "stream": stream or ident,
        "attrs": attrs,
    }
    with lock:
        starts[ident] = record
    write(path_of(ident), span_page(record, None, []))
    for listener in listeners:
        listener.heard_span(record)
    return Span(ident, stream or ident, project)


def opened(kind: Kind, name: str, project: str, parent: str | None, attrs: dict[str, JSON]) -> Span:
    return started(kind, name, project, parent, attrs, None, os.getpid(), time.time())


def child(parent: Span, kind: Kind, name: str, attrs: dict[str, JSON]) -> Span:
    return started(kind, name, parent.project, parent.id, attrs, parent.stream, os.getpid(), time.time())


def closed(span: Span) -> None:
    closed_at(span, time.time())


def closed_at(span: Span, stamp: float) -> None:
    record: SpanRecord = {"id": span.id, "event": "end", "time": stamp, "pid": os.getpid(), "ok": span.ok, "attrs": span.attrs}
    with lock:
        start = starts.pop(span.id, None)
        said: list[ChunkData] = []
        if span.stream == span.id:
            said = streams.pop(span.stream, [])
    if start is None:
        start, _, earlier = records_of(read(path_of(span.id)))
        said = earlier + said
    write(path_of(span.id), span_page(start, record, said))
    for listener in listeners:
        listener.heard_span(record)


def chunk(span: Span, kind: ChunkKind, text: str) -> None:
    chunk_at(span, kind, text, time.time())


def chunk_at(span: Span, kind: ChunkKind, text: str, stamp: float) -> None:
    if text != "":
        data: ChunkData = {"span": span.id, "time": stamp, "kind": kind, "text": text}
        with lock:
            held = streams.setdefault(span.stream, [])
            if len(held) > 0 and held[-1]["span"] == span.id and held[-1]["kind"] == kind:
                held[-1] = {"span": span.id, "time": held[-1]["time"], "kind": kind, "text": held[-1]["text"] + text}
            else:
                held.append(data)
        for listener in listeners:
            listener.heard_chunk(data)


@contextmanager
def span(kind: Kind, name: str, project: str, parent: str | None, attrs: dict[str, JSON]) -> Iterator[Span]:
    made = opened(kind, name, project, parent, attrs)
    token = current.set(made.id)
    try:
        yield made
    except BaseException as error:
        made.ok = False
        made.attrs["reason"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        current.reset(token)
        closed(made)


def chunks(stream: str) -> list[ChunkData]:
    with lock:
        held = list(streams.get(stream, []))
    if len(held) > 0 or not path_of(stream).is_file():
        return held
    else:
        return records_of(read(path_of(stream)))[2]


def days(back: int) -> list[Path]:
    found: list[Path] = []
    if SPANS.is_dir():
        for folder in sorted(SPANS.iterdir())[-back:]:
            found.extend(sorted(folder.glob("*.md"), key=lambda path: int(path.stem)))
    return found


def recorded(back: int) -> list[SpanRecord]:
    found: list[SpanRecord] = []
    for path in days(back):
        if path.stat().st_size > 0:
            try:
                start, end, _ = records_of(read(path))
            except Unreadable:
                pass
            else:
                found.append(start)
                if end is not None:
                    found.append(end)
    return found


def project_of(cwd: str) -> str:
    parts = Path(cwd).parts
    if LAIN in parts:
        return str(Path(*parts[: len(parts) - 1 - parts[::-1].index(LAIN)]))
    else:
        return cwd


@dataclass
class Called:
    start: SpanRecord
    end: SpanRecord | None

    def seconds(self, now: float) -> float:
        if self.end is None:
            return now - self.start["time"]
        else:
            return self.end["time"] - self.start["time"]

    def given(self, key: str) -> JSON:
        return self.start.get("attrs", {}).get(key)

    def spent(self, key: str) -> int:
        if self.end is None:
            return 0
        else:
            value = self.end.get("attrs", {}).get(key)
            if isinstance(value, int):
                return value
            else:
                return 0

    def timed(self, key: str) -> float | None:
        if self.end is None:
            return None
        else:
            value = self.end.get("attrs", {}).get(key)
            if isinstance(value, int | float):
                return float(value)
            else:
                return None


def called(found: list[SpanRecord]) -> list[Called]:
    shown: list[Called] = []
    by: dict[str, Called] = {}
    for record in found:
        if record["event"] == "start" and record.get("kind") == "call":
            each = Called(record, None)
            by[record["id"]] = each
            shown.append(each)
        elif record["event"] == "end" and record["id"] in by:
            by[record["id"]].end = record
    return shown


def merged(found: list[Called], now: float) -> list[tuple[float, float]]:
    runs: list[tuple[float, float]] = []
    for each in sorted(found, key=lambda one: one.start["time"]):
        begin = each.start["time"]
        finish = begin + each.seconds(now)
        if len(runs) > 0 and begin <= runs[-1][1]:
            runs[-1] = (runs[-1][0], max(runs[-1][1], finish))
        else:
            runs.append((begin, finish))
    return runs


def rate(tokens: int, seconds: float) -> str:
    if seconds <= 0:
        return "-"
    else:
        return f"{tokens / seconds:.1f}"


def middle(values: list[float]) -> str:
    if len(values) == 0:
        return "-"
    else:
        return f"{statistics.median(values):.1f}s"


def tally(label: str, found: list[Called], now: float) -> list[str]:
    walls: list[float] = []
    firsts: list[float] = []
    api = 0.0
    outside = 0.0
    timed = 0.0
    prompt = 0
    cached = 0
    written = 0
    completion = 0
    thinking = 0
    turns = 0
    done = 0
    for each in found:
        if each.end is not None and each.end.get("ok", False):
            done += 1
            wall = each.seconds(now)
            walls.append(wall)
            first = each.timed("first")
            inside = each.timed("inside")
            if first is not None:
                firsts.append(first)
            if inside is not None:
                outside += wall - inside
                timed += wall
            api += each.timed("api_seconds") or 0.0
            prompt += each.spent("prompt")
            cached += each.spent("cached")
            written += each.spent("written")
            completion += each.spent("completion")
            thinking += each.spent("thinking")
            turns += each.spent("turns")
    lines = [
        f"{label}: {len(found)} calls, {len(found) - done} failed or running, median {middle(walls)}, first token after a median {middle(firsts)}, {turns} turns",
        f"  {prompt} tokens in ({cached} read from the cache, {written} written to it), {completion} out ({thinking} thinking)",
        f"  {rate(completion, sum(walls))} out/s over each call, {rate(completion, api)} out/s while in the API",
    ]
    if timed > 0:
        lines.append(f"  {outside:.0f}s of {timed:.0f}s spent outside Claude Code's own timing (starting the process, reading the answer)")
    return lines


def gaps(found: list[Called], now: float) -> list[str]:
    by: dict[str, list[Called]] = {}
    for each in found:
        by.setdefault(str(each.given("command")), []).append(each)
    lines: list[str] = []
    for command, group in sorted(by.items()):
        runs = merged(group, now)
        between: list[float] = []
        for earlier, later in zip(runs, runs[1:]):
            between.append(later[0] - earlier[1])
        if len(between) > 0:
            when = f"{time.strftime('%H:%M:%S', time.localtime(runs[0][0]))} to {time.strftime('%H:%M:%S', time.localtime(runs[-1][1]))}"
            lines.append(f"{command}, {when}: {len(group)} calls, no call running for {sum(between):.0f}s in {len(between)} gaps, longest {max(between):.0f}s")
    return lines


def largest(found: list[Called], now: float) -> list[str]:
    ranked = sorted(found, key=lambda each: each.spent("prompt"), reverse=True)[:LARGEST]
    lines: list[str] = []
    for each in ranked:
        when = time.strftime("%H:%M:%S", time.localtime(each.start["time"]))
        lines.append(f"  {when} {each.given('model')} {each.given('purpose')} {each.given('subject')}: {each.spent('prompt')} in over {each.seconds(now):.0f}s")
    if len(lines) > 0:
        lines.insert(0, "the calls with the most tokens in:")
    return lines


def summary(found: list[Called], now: float) -> list[str]:
    groups: dict[str, list[Called]] = {}
    for each in found:
        groups.setdefault(f"{each.given('api')} {each.given('model')} {each.given('purpose')}", []).append(each)
    lines: list[str] = []
    for key in sorted(groups):
        lines.extend(tally(key, groups[key], now))
    if len(found) > 0:
        window = now - found[0].start["time"]
        busy = 0.0
        for begin, finish in merged(found, now):
            busy += finish - begin
        lines.append(f"over {window:.0f}s some call was in flight for {busy:.0f}s")
    lines.extend(gaps(found, now))
    lines.extend(largest(found, now))
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="spans.py", description="Say how fast the model calls in the install's span log went, and where the time between them went.")
    parser.add_argument("--project", default="", help="only calls whose project contains this")
    parser.add_argument("--hours", type=float, default=1.0, help="how far back to look")
    parsed = parser.parse_args(argv)
    project: str = parsed.project
    hours: float = parsed.hours
    now = time.time()
    found: list[Called] = []
    lost = 0
    for each in called(recorded(int(hours // 24) + 2)):
        if each.start["time"] < now - hours * 3600 or project not in each.start.get("project", ""):
            pass
        elif each.end is None and not alive(each.start["pid"]):
            lost += 1
        else:
            found.append(each)
    if lost > 0:
        print(f"{lost} calls ended with their process and are left out")
    for line in summary(found, now):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
