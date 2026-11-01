from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, Required, TypedDict, cast
from urllib.parse import parse_qs, urlsplit

import spans
from bugs import caught
from build import Broken, build, chosen, current, prune
from config import SETTINGS, Settings, address
from hook import HookInput, Threaded, followed, handle, install, work
from ingest import LIVE, alive, session_name, stamp_of
from jobs import Jobs, job_named
from mcp import Served, answer, register
from model import Failure
from page import Builder, Page, items, number, read, write, write_items
from places import CHECKOUT, CODE, INSTALL, LAUNCHER, Places, in_checkout, places, repo_of
from rpc import FlightData, FlightsData, NodeData, OpenedData, ProjectData, Request, RootsData, SpanState, TallyData, WhoData, answered, asked, posted
from spans import ChunkData, Span, SpanRecord, chunk, chunk_at, closed, closed_at, cut, started
from store import JSON, locked, record_of, replace_text, unless_held
from transcript import Record, blocks
from upkeep import due

WEB = CODE / "web"
RECORD = "daemon.md"
LOG = "daemon.log"
KNOWN = "projects.md"
SWEEP_S = 10.0
KEPT = 5000
RECENT_S = 3600.0
DAYS = 2
HEARTBEAT_S = 15.0
WAIT_S = 15.0
SERVICE = Path.home() / ".config" / "systemd" / "user" / "lain.service"
USER_SETTINGS = Path.home() / ".claude" / "settings.json"

type Scope = Literal["user", "project"]


class LiveEvent(TypedDict, total=False):
    seq: Required[int]
    type: Required[Literal["span", "chunk"]]
    project: Required[str]
    span: NodeData
    chunk: ChunkData


@dataclass
class Node:
    start: SpanRecord
    end: SpanRecord | None
    state: SpanState
    ended: float | None
    children: list[str] = field(default_factory=list)


@dataclass
class Session:
    span: Span
    pid: int
    transcript: Path
    offset: int
    prompt: Span | None = None


class Bus:
    events: deque[LiveEvent]
    seq: int
    cond: threading.Condition

    def __init__(self) -> None:
        self.events = deque(maxlen=KEPT)
        self.seq = 0
        self.cond = threading.Condition()

    def emit(self, event: LiveEvent) -> None:
        with self.cond:
            self.seq += 1
            event["seq"] = self.seq
            self.events.append(event)
            self.cond.notify_all()

    def after(self, seq: int, wait: float) -> list[LiveEvent]:
        found: list[LiveEvent] = []
        with self.cond:
            if self.seq <= seq:
                self.cond.wait(wait)
            for event in self.events:
                if event["seq"] > seq:
                    found.append(event)
        return found


def first(text: str, limit: int = 160) -> str:
    line = text.strip().partition("\n")[0]
    if len(line) > limit:
        return f"{line[:limit]}..."
    else:
        return line


def touched(root: Path, data: HookInput) -> str:
    given = data.get("tool_input", {})
    for key in ("file_path", "notebook_path", "path"):
        value = given.get(key)
        if isinstance(value, str) and value != "":
            path = Path(data.get("cwd", "")) / value
            if path.is_relative_to(root):
                return path.relative_to(root).as_posix()
    return ""


def covered(found: list[tuple[float, float]]) -> float:
    total = 0.0
    reach = 0.0
    for begin, finish in sorted(found):
        start = max(begin, reach)
        if finish > start:
            total += finish - start
            reach = finish
    return total


def tally_for(apis: dict[str, TallyData], api: str) -> TallyData:
    return apis.setdefault(api, {"running": 0, "done": 0, "failed": 0, "prompt": 0, "cached": 0, "completion": 0, "seconds": 0.0, "busy": 0.0})


def given_int(attrs: dict[str, JSON], key: str) -> int:
    value = attrs.get(key)
    if isinstance(value, int):
        return value
    else:
        return 0


def tool_text(value: JSON) -> str:
    if isinstance(value, str):
        return value
    else:
        return json.dumps(value, ensure_ascii=False)


def live_pid(found: Places, session: str) -> int | None:
    path = found.agent(session) / LIVE
    if path.is_file():
        return number(read(path).need("process"))
    else:
        return None


class Lain:
    settings: Settings
    port: int
    home: Path
    started: float
    bus: Bus
    sites: dict[str, Places]
    lock: threading.Lock
    tracer: Threaded
    pool: ThreadPoolExecutor
    indexing: threading.Lock
    nodes: dict[str, Node]
    roots: list[str]
    heard: dict[int, float]
    sessions: dict[str, Session]
    jobs: Jobs
    server: Server | None

    def __init__(self, settings: Settings, port: int, home: Path) -> None:
        self.settings = settings
        self.jobs = Jobs(settings)
        self.server = None
        self.port = port
        self.home = home
        self.started = time.time()
        self.bus = Bus()
        self.sites = {}
        self.lock = threading.Lock()
        self.tracer = Threaded()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.indexing = threading.Lock()
        self.nodes = {}
        self.roots = []
        self.heard = {}
        self.sessions = {}
        for root in items(home / KNOWN, "project"):
            self.site(Path(root))
        with self.indexing:
            for record in spans.recorded(DAYS):
                self.take(record, False)
        self.lost(False)
        spans.listeners.append(self)

    def site(self, directory: Path) -> Places | None:
        if directory.is_dir() and in_checkout(directory.resolve()):
            return self.site_at(repo_of(directory.resolve()).root)
        else:
            return None

    def site_at(self, root: Path) -> Places:
        key = str(root)
        with self.lock:
            if key not in self.sites:
                self.sites[key] = places(root)
                write_items(self.home / KNOWN, "the projects lain has served", "project", sorted(self.sites))
            return self.sites[key]

    def served(self, cwd: str) -> Served | None:
        found: Places | None = None
        if cwd != "":
            found = self.site(Path(cwd))
        if found is None:
            return None
        else:
            return Served(self, found)

    def hooked(self, data: HookInput) -> str | None:
        cwd = Path(data.get("cwd", ""))
        if cwd.is_dir() and followed(cwd.resolve()):
            found = self.site(cwd)
            if found is None:
                return None
            else:
                self.told(found, data)
                return handle(self.settings, data, self.tracer, self.later)
        else:
            return None

    def session_of(self, found: Places, data: HookInput) -> Session:
        ident = data.get("session_id", "")
        known = self.sessions.get(ident)
        if known is not None:
            return known
        else:
            name = session_name(found, ident, time.time())
            pid = data.get("claude_pid") or live_pid(found, name) or os.getpid()
            transcript = Path(data.get("transcript_path", ""))
            offset = 0
            if transcript.is_file():
                offset = transcript.stat().st_size
            running = self.running_session(ident)
            if running is not None:
                made = Span(running, running, str(found.repo.root))
            else:
                given: dict[str, JSON] = {"session": ident, "cwd": data.get("cwd", ""), "transcript": str(transcript)}
                made = started("session", name, str(found.repo.root), None, given, None, pid, time.time())
            session = Session(made, pid, transcript, offset)
            self.sessions[ident] = session
            return session

    def running_session(self, ident: str) -> str | None:
        found: str | None = None
        with self.indexing:
            for node in self.nodes.values():
                if node.state == "running" and node.start.get("kind") == "session" and node.start.get("attrs", {}).get("session") == ident:
                    found = node.start["id"]
        return found

    def told(self, found: Places, data: HookInput) -> None:
        ident = data.get("session_id", "")
        with self.lock:
            if data.get("hook_event_name") != "SessionEnd" or ident in self.sessions or self.running_session(ident) is not None:
                self.recorded(found, data)

    def recorded(self, found: Places, data: HookInput) -> None:
        session = self.session_of(found, data)
        now = time.time()
        match data.get("hook_event_name"):
            case "UserPromptSubmit":
                self.read_transcript(session)
                self.end_prompt(session)
                prompt = data.get("prompt", "")
                session.prompt = started("prompt", first(prompt), session.span.project, session.span.id, {"prompt": prompt}, None, session.pid, now)
            case "PostToolUse" | "PostToolUseFailure":
                self.read_transcript(session)
                parent = session.span
                if session.prompt is not None:
                    parent = session.prompt
                given: dict[str, JSON] = {"input": cut(tool_text(data.get("tool_input", {}))), "id": data.get("tool_use_id", "")}
                path = touched(found.repo.root, data)
                if path != "":
                    given["path"] = path
                when = now - data.get("duration_ms", 0) / 1000
                tool = started("tool", data.get("tool_name", ""), parent.project, parent.id, given, parent.stream, session.pid, when)
                chunk(tool, "output", cut(tool_text(data.get("tool_response", ""))))
                tool.ok = data.get("hook_event_name") == "PostToolUse"
                closed_at(tool, now)
            case "Stop":
                self.read_transcript(session)
                self.end_prompt(session)
            case "SessionEnd":
                self.read_transcript(session)
                self.end_prompt(session)
                closed(session.span)
                self.sessions.pop(data.get("session_id", ""), None)
            case _:
                pass

    def end_prompt(self, session: Session) -> None:
        if session.prompt is not None:
            closed(session.prompt)
            session.prompt = None

    def read_transcript(self, session: Session) -> None:
        if session.transcript.is_file():
            size = session.transcript.stat().st_size
            if size > session.offset:
                with session.transcript.open("rb") as file:
                    file.seek(session.offset)
                    read = file.read(size - session.offset)
                end = read.rfind(b"\n") + 1
                session.offset += end
                for line in read[:end].decode(errors="replace").split("\n"):
                    self.said(session, line)

    def said(self, session: Session, line: str) -> None:
        data = record_of(line)
        if data is not None:
            record = cast(Record, data)
            target = session.span
            if session.prompt is not None:
                target = session.prompt
            if record.get("type") == "assistant" and record.get("isSidechain") is not True and "timestamp" in record:
                when = stamp_of(record)
                for block in blocks(record.get("message", {})):
                    match block.get("type"):
                        case "text":
                            chunk_at(target, "text", block.get("text", ""), when)
                        case "thinking":
                            chunk_at(target, "thinking", block.get("thinking", ""), when)
                        case _:
                            pass

    def later(self, data: HookInput) -> None:
        self.pool.submit(self.working, data)

    def latest_session(self, ident: str) -> str | None:
        found: str | None = None
        with self.indexing:
            for node in self.nodes.values():
                if node.start.get("kind") == "session" and node.start.get("attrs", {}).get("session") == ident:
                    found = node.start["id"]
        return found

    def working(self, data: HookInput) -> None:
        parent = self.latest_session(data.get("session_id", ""))
        project = str(repo_of(Path(data.get("cwd", "."))).root)
        try:
            with spans.span("work", f"read in after {data.get('hook_event_name', '')}", project, parent, {}) as doing:
                doing.ok = work(self.settings, data) == 0
            if data.get("hook_event_name") in ("Stop", "SessionEnd"):
                self.upkept(Path(project))
        except Exception as error:
            caught(error, "work", project, json.dumps(data, ensure_ascii=False))

    def upkept(self, root: Path) -> None:
        found = self.site(root)
        if found is not None:
            pick = due(found, self.settings)
            if pick is not None:
                self.jobs.start(found, "recheck", pick)

    def heard_span(self, record: SpanRecord) -> None:
        with self.indexing:
            self.take(record, True)

    def heard_chunk(self, piece: ChunkData) -> None:
        self.bus.emit({"seq": 0, "type": "chunk", "project": self.chunk_project(piece), "chunk": piece})

    def take(self, record: SpanRecord, telling: bool) -> None:
        self.heard[record["pid"]] = record["time"]
        ident = record["id"]
        if record["event"] == "start" and ident not in self.nodes:
            node = Node(record, None, "running", None)
            self.nodes[ident] = node
            parent = record.get("parent")
            if parent is not None and parent in self.nodes:
                self.nodes[parent].children.append(ident)
            else:
                self.roots.append(ident)
            if telling:
                self.told_span(node)
        elif record["event"] == "end" and ident in self.nodes and self.nodes[ident].state == "running":
            node = self.nodes[ident]
            node.end = record
            node.ended = record["time"]
            node.state = "failed"
            if record.get("ok", False):
                node.state = "ok"
            if telling:
                self.told_span(node)

    def told_span(self, node: Node) -> None:
        shown = self.shown(node)
        self.bus.emit({"seq": 0, "type": "span", "project": shown["project"], "span": shown})

    def project(self, node: Node) -> str:
        at: Node | None = node
        while at is not None:
            given = at.start.get("project", "")
            if given != "":
                return given
            else:
                parent = at.start.get("parent")
                at = None
                if parent is not None:
                    at = self.nodes.get(parent)
        return ""

    def depth(self, node: Node) -> int:
        count = 0
        parent = node.start.get("parent")
        while parent is not None and parent in self.nodes:
            count += 1
            parent = self.nodes[parent].start.get("parent")
        return count

    def running_under(self, node: Node) -> int:
        count = 0
        for ident in node.children:
            each = self.nodes[ident]
            if each.state == "running":
                count += 1
            count += self.running_under(each)
        return count

    def shown(self, node: Node) -> NodeData:
        attrs = dict(node.start.get("attrs", {}))
        if node.end is not None:
            attrs.update(node.end.get("attrs", {}))
        start = node.start
        return {
            "id": start["id"],
            "parent": start.get("parent"),
            "kind": start.get("kind", ""),
            "name": start.get("name", ""),
            "project": self.project(node),
            "pid": start["pid"],
            "started": start["time"],
            "ended": node.ended,
            "state": node.state,
            "attrs": attrs,
            "stream": start.get("stream", start["id"]),
            "children": len(node.children),
            "running": self.running_under(node),
            "depth": self.depth(node),
        }

    def lost(self, telling: bool) -> None:
        with self.indexing:
            for node in self.nodes.values():
                if node.state == "running" and not alive(node.start["pid"]):
                    node.state = "lost"
                    node.ended = max(node.start["time"], self.heard.get(node.start["pid"], node.start["time"]))
                    if telling:
                        self.told_span(node)

    def chunk_project(self, piece: ChunkData) -> str:
        with self.indexing:
            node = self.nodes.get(piece["span"])
            if node is None:
                return ""
            else:
                return self.project(node)

    def watch(self) -> None:
        while True:
            time.sleep(SWEEP_S)
            try:
                self.lost(True)
                self.tracer.sweep()
            except Exception as error:
                caught(error, "watch", "", "")

    def listed(self, project: str, before: float, limit: int) -> RootsData:
        found: list[NodeData] = []
        with self.indexing:
            seq = self.bus.seq
            for ident in reversed(self.roots):
                node = self.nodes[ident]
                if len(found) < limit and (before == 0 or node.start["time"] < before) and project in ("", self.project(node)):
                    found.append(self.shown(node))
        return {"seq": seq, "spans": found}

    def opened(self, ident: str) -> OpenedData | None:
        with self.indexing:
            node = self.nodes.get(ident)
            if node is None:
                return None
            else:
                shown = self.shown(node)
                children: list[NodeData] = []
                for each in node.children:
                    children.append(self.shown(self.nodes[each]))
        pieces: list[ChunkData] = []
        for piece in spans.chunks(shown["stream"]):
            if piece["span"] == ident:
                pieces.append(piece)
        return {"span": shown, "children": children, "chunks": pieces}

    def flown(self) -> FlightsData:
        apis: dict[str, TallyData] = {}
        running: list[FlightData] = []
        busy: dict[str, list[tuple[float, float]]] = {}
        now = time.time()
        since = now - RECENT_S
        with self.indexing:
            for node in self.nodes.values():
                if node.start.get("kind") == "call" and (node.ended is None or node.ended >= since):
                    attrs = self.shown(node)["attrs"]
                    api = str(attrs.get("api", ""))
                    tally = tally_for(apis, api)
                    finish = node.ended or now
                    busy.setdefault(api, []).append((max(node.start["time"], since), finish))
                    if node.state == "running":
                        tally["running"] += 1
                        flight: FlightData = {
                            "id": node.start["id"],
                            "api": api,
                            "model": str(attrs.get("model", "")),
                            "purpose": str(attrs.get("purpose", "")),
                            "subject": str(attrs.get("subject", "")),
                            "project": self.project(node),
                            "started": node.start["time"],
                        }
                        running.append(flight)
                    else:
                        if node.state == "ok":
                            tally["done"] += 1
                        else:
                            tally["failed"] += 1
                        tally["prompt"] += given_int(attrs, "prompt")
                        tally["cached"] += given_int(attrs, "cached")
                        tally["completion"] += given_int(attrs, "completion")
                        tally["seconds"] += finish - node.start["time"]
        for api, found in busy.items():
            apis[api]["busy"] = covered(found)
        running.sort(key=lambda each: each["started"])
        return {"running": running, "apis": apis}

    def projects(self) -> list[ProjectData]:
        shown: list[ProjectData] = []
        for key, found in sorted(self.sites.items()):
            shown.append({"root": key, "name": found.repo.name, "running": self.jobs.busy(found)})
        return shown

    def who(self) -> WhoData:
        return {"lain": str(CHECKOUT), "build": CODE.name, "pid": os.getpid(), "port": self.port, "started": self.started}

    def quit(self) -> None:
        for found in list(self.sites.values()):
            for name in self.jobs.busy(found):
                job = job_named(name)
                if job is not None:
                    self.jobs.stop(found, job)
        if self.server is not None:
            threading.Thread(target=self.server.shutdown, daemon=True).start()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: socket.socket | tuple[bytes, socket.socket], client_address: tuple[str, int]) -> None:
        error = sys.exception()
        if error is not None and not isinstance(error, ConnectionError):
            caught(error, "request", "", "")


def given(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    if len(values) == 0:
        return ""
    else:
        return values[0]


def project_in(query: dict[str, list[str]], raw: bytes) -> str:
    data = record_of(raw.decode(errors="replace")) or {}
    for value in (given(query, "cwd"), data.get("cwd"), data.get("project")):
        if isinstance(value, str) and value != "":
            return value
    return ""


def mcp_where(request: dict[str, JSON]) -> str:
    params = request.get("params")
    if isinstance(params, dict) and isinstance(params.get("name"), str):
        return f"mcp {params['name']}"
    else:
        return f"mcp {request['method']}"


class Handler(BaseHTTPRequestHandler):
    lain: Lain

    def log_message(self, format: str, *args: str | int) -> None:
        pass

    def trusted(self) -> bool:
        port = self.lain.port
        here = (f"127.0.0.1:{port}", f"localhost:{port}")
        origin = self.headers.get("Origin")
        return self.headers.get("Host") in here and (origin is None or origin in (f"http://{here[0]}", f"http://{here[1]}"))

    def body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_POST(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        raw = self.body()
        try:
            match parts.path:
                case "/rpc" if self.trusted():
                    self.rpc(cast(Request, json.loads(raw)))
                case "/hook" if self.trusted():
                    said = self.lain.hooked(cast(HookInput, json.loads(raw)))
                    self.send((said or "").encode(), "application/json")
                case "/mcp" if self.trusted():
                    self.mcp(given(query, "cwd"), raw)
                case "/rpc" | "/hook" | "/mcp":
                    self.send_error(403)
                case _:
                    self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            reason = caught(error, f"POST {parts.path}", project_in(query, raw), raw.decode(errors="replace"))
            if parts.path == "/hook":
                self.send(b"", "application/json")
            else:
                self.send(json.dumps({"error": reason}).encode(), "application/json", 500)

    def rpc(self, request: Request) -> None:
        found: Places | None = None
        if request["project"] != "":
            found = self.lain.site(Path(request["project"]))
        reply = answered(self.lain, found, request)
        self.send(json.dumps(reply, ensure_ascii=False).encode(), "application/json")

    def mcp(self, cwd: str, raw: bytes) -> None:
        try:
            request: JSON = json.loads(raw)
        except json.JSONDecodeError as error:
            self.send(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"not JSON: {error}"}}).encode(), "application/json")
        else:
            self.mcp_request(cwd, request)

    def mcp_request(self, cwd: str, request: JSON) -> None:
        if not isinstance(request, dict) or "method" not in request:
            self.send(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "a message is one JSON object with a method"}}).encode(), "application/json")
        elif "id" not in request:
            self.send(b"", "application/json", 202)
        else:
            try:
                found = answer(self.lain.served(cwd), request)
            except Exception as error:
                reason = caught(error, mcp_where(request), cwd, json.dumps(request, ensure_ascii=False))
                found = {"error": {"code": -32603, "message": f"lain failed: {reason}"}}
            self.send(json.dumps({"jsonrpc": "2.0", "id": request["id"], **found}, ensure_ascii=False).encode(), "application/json")

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            self.got(parts.path, query)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            reason = caught(error, f"GET {parts.path}", given(query, "project"), "")
            self.send(json.dumps({"error": reason}).encode(), "application/json", 500)

    def got(self, path: str, query: dict[str, list[str]]) -> None:
        match path:
            case _ if not self.trusted():
                self.send_error(403)
            case "/":
                self.send((WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
            case "/lain.js":
                self.send((WEB / "lain.js").read_bytes(), "text/javascript; charset=utf-8")
            case "/live":
                self.live(given(query, "project"), number_in(given(query, "since")))
            case _:
                self.send_error(404)

    def send(self, body: bytes, kind: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def live(self, project: str, since: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seq = since
        while True:
            fresh = self.lain.bus.after(seq, HEARTBEAT_S)
            if len(fresh) == 0:
                self.wfile.write(b": still here\n\n")
            for event in fresh:
                if project in ("", event["project"]):
                    self.wfile.write(f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode())
                seq = event["seq"]
            self.wfile.flush()


def number_in(text: str) -> int:
    if text.isdigit():
        return int(text)
    else:
        return 0


def whoami(port: int) -> WhoData | None:
    reply = asked(port, "whoami", "", {}, 2)
    if reply is None or "result" not in reply:
        return None
    else:
        return cast(WhoData, reply["result"])


def stop(port: int) -> bool:
    return asked(port, "quit", "", {}, 5) is not None


def waited(port: int, answering: bool) -> bool:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if (whoami(port) is not None) == answering:
            return True
        else:
            time.sleep(0.1)
    return False


def ensure(port: int) -> bool:
    if whoami(port) is not None:
        return True
    else:
        INSTALL.mkdir(parents=True, exist_ok=True)
        with (INSTALL / LOG).open("a") as log:
            subprocess.Popen([sys.executable, str(LAUNCHER), "--port", str(port), "serve"], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        return waited(port, True)


def forward(port: int) -> int:
    data: dict[str, JSON] = json.loads(sys.stdin.read())
    data["claude_pid"] = os.getppid()
    if ensure(port):
        said = posted(port, "/hook", json.dumps(data).encode(), 60)
        if said is not None and said.strip() != b"":
            sys.stdout.write(said.decode())
    return 0


def serve(settings: Settings, port: int, home: Path) -> int:
    lock = home / "daemon" / str(port)
    with unless_held(lock) as mine:
        if not mine:
            print(f"lain is already running on port {port}; this one takes over once it stops", file=sys.stderr, flush=True)
    with locked(lock):
        return listened(Lain(settings, port, home))


def listened(lain: Lain) -> int:
    handler = type("Bound", (Handler,), {"lain": lain})
    try:
        server = Server(("127.0.0.1", lain.port), handler)
    except OSError as error:
        print(f"lain could not listen on port {lain.port}: {error}", file=sys.stderr)
        return 1
    else:
        lain.server = server
        with server:
            who = lain.who()
            built = Builder(f"lain's process {who['pid']} on port {who['port']}")
            for key, value in who.items():
                built.field(key, str(value))
            write(lain.home / RECORD, built.page())
            threading.Thread(target=lain.watch, daemon=True).start()
            print(f"lain serving at {address(lain.port)}", flush=True)
            server.serve_forever()
        return 0


def unit() -> str:
    return "\n".join(
        [
            "[Unit]",
            "Description=lain: one process for every project's hooks, MCP server, viewer and jobs",
            "",
            "[Service]",
            f"ExecStart={sys.executable} {LAUNCHER} serve",
            "Restart=always",
            "RestartSec=2",
            "",
            "[Install]",
            "WantedBy=default.target",
            "",
        ]
    )


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)


def service(port: int, adding: bool) -> str:
    if shutil.which("systemctl") is None:
        return "no systemd here, so lain starts when a session starts or the CLI is used"
    elif adding:
        stop(port)
        replace_text(SERVICE, unit())
        systemctl("daemon-reload")
        systemctl("enable", "lain.service")
        done = systemctl("restart", "lain.service")
        if done.returncode != 0:
            return f"systemd could not start lain: {(done.stderr or done.stdout).strip()}"
        else:
            return f"lain runs as the systemd user service {SERVICE.name}, restarted if it stops"
    elif SERVICE.is_file():
        systemctl("disable", "--now", "lain.service")
        SERVICE.unlink()
        systemctl("daemon-reload")
        return "removed lain's systemd user service"
    else:
        stop(port)
        return "stopped lain"


def service_state() -> str:
    if shutil.which("systemctl") is None:
        return "no systemd here, so lain starts when a session starts or the CLI is used"
    elif not SERVICE.is_file():
        return "not a systemd user service; it starts when a session starts or the CLI is used"
    else:
        running = systemctl("is-active", "lain.service").stdout.strip()
        started = ""
        for line in SERVICE.read_text().splitlines():
            if line.startswith("ExecStart="):
                started = line.removeprefix("ExecStart=").partition(" ")[0]
        return f"the systemd user service {SERVICE.name}, {running}, run with {started}"


def claude_install(project: Path, scope: Scope, adding: bool, port: int) -> int:
    if scope == "user":
        print(install(USER_SETTINGS, adding, port))
        served = register("user", project, adding, port)
        print(service(port, adding))
    else:
        print(install(project / ".claude" / "settings.local.json", adding, port))
        served = register("local", project, adding, port)
        if adding and not ensure(port):
            print("lain did not start; it starts with the next session", file=sys.stderr)
    if isinstance(served, Failure):
        print(served.reason, file=sys.stderr)
        return 1
    else:
        print(served)
        return 0


def replaced(port: int, before: int) -> bool:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        who = whoami(port)
        if who is None or who["pid"] != before:
            return True
        else:
            time.sleep(0.1)
    return False


def quit_from(port: int, who: WhoData | None) -> bool:
    if who is None:
        return True
    else:
        stop(port)
        return replaced(port, who["pid"])


def restart(port: int) -> bool:
    if not quit_from(port, whoami(port)):
        return False
    elif SERVICE.is_file() and port == SETTINGS.port:
        systemctl("restart", "lain.service")
        return waited(port, True)
    else:
        return ensure(port)


def kept_builds(made: Path, before: Path | None) -> tuple[Path, ...]:
    if before is None:
        return (made,)
    else:
        return (made, before)


def rebuilt(port: int) -> str | Failure:
    before = current()
    made = build()
    if isinstance(made, Broken):
        return Failure(f"lain was not rebuilt, so the lain running now is left as it is: {made.reason}")
    elif restart(port):
        prune(kept_builds(made, before))
        return f"lain runs build {made.name} of {CHECKOUT}"
    elif before is None or before == made:
        return Failure(f"build {made.name} did not start; see {INSTALL / LOG}")
    else:
        chosen(before)
        if restart(port):
            prune(kept_builds(made, before))
            return Failure(f"build {made.name} did not start, so lain runs the earlier build {before.name} again; see {INSTALL / LOG}")
        else:
            return Failure(f"build {made.name} did not start, and neither did the earlier build {before.name}; see {INSTALL / LOG}")
