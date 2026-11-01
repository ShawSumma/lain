from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from jobs import NAMES, Job, StatusData, described, job_named, millis
from model import Failure
from places import Places
from prompts import message, tool_text
from rpc import OFFERED, Host, called
from spec import Tool, parameters
from store import JSON

type McpScope = Literal["user", "local"]

NAME = "lain"
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MODERN = "2026-07-28"
VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_KEY = "io.modelcontextprotocol/clientCapabilities"
SERVER_KEY = "io.modelcontextprotocol/serverInfo"
TASKS = "io.modelcontextprotocol/tasks"
POLL_MS = 5000
LIST_TTL_MS = 3_600_000
PROMPTS = (*NAMES, "jobs", "page")
SERVED = {"name": NAME, "version": "1"}


@dataclass(frozen=True)
class Served:
    host: Host
    places: Places


def listed(tool: Tool) -> dict[str, JSON]:
    return {"name": tool.name, "description": tool_text(tool.name), "inputSchema": json.loads(json.dumps(parameters(tool)))}


def prompted(name: str) -> str:
    match name:
        case "jobs":
            return message("jobs_prompt")
        case "page":
            return message("page_prompt")
        case _:
            return message("job_prompt", job=name, about=message(f"job_{name}"))


def meta_of(params: dict[str, JSON]) -> dict[str, JSON]:
    meta = params.get("_meta")
    if isinstance(meta, dict):
        return meta
    else:
        return {}


def tasks_wanted(params: dict[str, JSON]) -> bool:
    capabilities = meta_of(params).get(CLIENT_KEY)
    if isinstance(capabilities, dict):
        extensions = capabilities.get("extensions")
        return isinstance(extensions, dict) and TASKS in extensions
    else:
        return False


def iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def task_id(name: str, started: float) -> str:
    return f"{name}-{millis(started)}"


def job_of(ident: str) -> Job | None:
    return job_named(ident.rpartition("-")[0])


def task_of(served: Served, ident: str) -> dict[str, JSON] | None:
    name = job_of(ident)
    stamp = ident.rpartition("-")[2]
    if name is not None and stamp.isdigit():
        return task_shown(ident, served.host.jobs.status_of_run(served.places, name, int(stamp)))
    else:
        return None


def last_updated(shown: StatusData) -> float:
    if shown["ended"] is None:
        return time.time()
    else:
        return shown["ended"]


def task_shown(ident: str, shown: StatusData | None) -> dict[str, JSON] | None:
    if shown is None or shown["started"] is None:
        return None
    else:
        task: dict[str, JSON] = {
            "taskId": ident,
            "createdAt": iso(shown["started"]),
            "lastUpdatedAt": iso(last_updated(shown)),
            "ttlMs": None,
            "pollIntervalMs": POLL_MS,
        }
        text = described(shown)
        match shown["state"]:
            case "running":
                task["status"] = "working"
                task["statusMessage"] = text.partition("\n")[0]
            case "finished":
                task["status"] = "completed"
                task["result"] = {"content": [{"type": "text", "text": f"{text}\n{message('job_done')}"}], "isError": False}
            case "failed":
                task["status"] = "failed"
                task["error"] = {"code": -32603, "message": text}
            case _:
                task["status"] = "cancelled"
                task["statusMessage"] = text.partition("\n")[0]
        return task


def started_task(served: Served, name: Job) -> dict[str, JSON]:
    begun = served.host.jobs.start(served.places, name, None)
    task = task_of(served, task_id(name, begun.time))
    if task is None:
        return {"content": [{"type": "text", "text": f"the {name} job did not start"}], "isError": True}
    else:
        return {"resultType": "task", **task}


def tasked(served: Served | None, method: str, params: dict[str, JSON]) -> dict[str, JSON]:
    ident = params.get("taskId")
    if served is not None and isinstance(ident, str):
        return task_answer(served, method, ident, task_of(served, ident), job_of(ident))
    else:
        return {"error": {"code": -32602, "message": f"no task {ident}"}}


def task_answer(served: Served, method: str, ident: str, task: dict[str, JSON] | None, job: Job | None) -> dict[str, JSON]:
    if task is None or job is None:
        return {"error": {"code": -32602, "message": f"no task {ident}"}}
    elif method == "tasks/get":
        return {"result": task}
    elif method == "tasks/cancel":
        served.host.jobs.stop(served.places, job)
        return {"result": {}}
    else:
        return {"result": {}}


def answer(served: Served | None, request: dict[str, JSON]) -> dict[str, JSON]:
    method = request.get("method")
    params = request.get("params")
    if not isinstance(params, dict):
        params = {}
    found = answered(served, str(method), params)
    result = found.get("result")
    if meta_of(params).get(VERSION_KEY) == MODERN and isinstance(result, dict):
        if "resultType" not in result:
            result["resultType"] = "complete"
        if method in ("tools/list", "prompts/list"):
            result["ttlMs"] = LIST_TTL_MS
            result["cacheScope"] = "private"
        result["_meta"] = {SERVER_KEY: SERVED}
    return found


def answered(served: Served | None, method: str, params: dict[str, JSON]) -> dict[str, JSON]:
    offered: tuple[Tool, ...] = ()
    prompts: tuple[str, ...] = ()
    if served is not None:
        offered = OFFERED
        prompts = PROMPTS
    capabilities: dict[str, JSON] = {"tools": {}, "prompts": {}}
    match method:
        case "initialize":
            asked = params.get("protocolVersion")
            version = VERSIONS[0]
            if isinstance(asked, str) and asked in VERSIONS:
                version = asked
            return {"result": {"protocolVersion": version, "capabilities": capabilities, "serverInfo": SERVED}}
        case "server/discover":
            capabilities["extensions"] = {TASKS: {}}
            return {"result": {"supportedVersions": [MODERN], "capabilities": capabilities, "ttlMs": LIST_TTL_MS, "cacheScope": "private"}}
        case "ping":
            return {"result": {}}
        case "prompts/list":
            listing: list[JSON] = []
            for name in prompts:
                listing.append({"name": name, "description": message(f"job_{name}"), "arguments": []})
            return {"result": {"prompts": listing}}
        case "prompts/get":
            name = params.get("name")
            if not isinstance(name, str) or name not in prompts:
                return {"error": {"code": -32602, "message": f"no prompt {name}"}}
            else:
                return {"result": {"description": message(f"job_{name}"), "messages": [{"role": "user", "content": {"type": "text", "text": prompted(name)}}]}}
        case "tools/list":
            tools: list[JSON] = []
            for tool in offered:
                tools.append(listed(tool))
            return {"result": {"tools": tools}}
        case "tools/call":
            return tool_called(served, offered, params)
        case "tasks/get" | "tasks/cancel" | "tasks/update":
            return tasked(served, method, params)
        case _:
            return {"error": {"code": -32601, "message": f"no method {method}"}}


def only_job(given: JSON) -> Job | None:
    if isinstance(given, list) and len(given) == 1 and isinstance(given[0], str):
        return job_named(given[0])
    else:
        return None


def offers(offered: tuple[Tool, ...], name: str) -> bool:
    for tool in offered:
        if tool.name == name:
            return True
    return False


def tool_called(served: Served | None, offered: tuple[Tool, ...], params: dict[str, JSON]) -> dict[str, JSON]:
    name = str(params.get("name"))
    args = params.get("arguments")
    if not isinstance(args, dict):
        args = {}
    job = only_job(args.get("jobs"))
    if served is None or not offers(offered, name):
        return {"error": {"code": -32602, "message": f"no tool {name}"}}
    elif name == "start" and job is not None and tasks_wanted(params):
        return {"result": started_task(served, job)}
    else:
        done = called(served.host, served.places, name, args)
        if isinstance(done, Failure):
            return {"result": {"content": [{"type": "text", "text": done.reason}], "isError": True}}
        else:
            return {"result": {"content": [{"type": "text", "text": done.text}], "isError": False}}


def claude_mcp(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | Failure:
    try:
        return subprocess.run(["claude", "mcp", *args], cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return Failure("claude is not installed, so lain's MCP server was not registered")


def endpoint(scope: McpScope, cwd: Path, port: int) -> str:
    if scope == "user":
        return f"http://127.0.0.1:{port}/mcp?cwd=${{PWD}}"
    else:
        return f"http://127.0.0.1:{port}/mcp?cwd={quote(str(cwd), safe='')}"


def registration(cwd: Path) -> str:
    found = claude_mcp(["get", NAME], cwd)
    if isinstance(found, Failure):
        return found.reason
    elif found.returncode != 0:
        return "lain's MCP server is not registered for this directory"
    else:
        lines: list[str] = []
        for line in found.stdout.splitlines():
            if line.startswith("  "):
                lines.append(line.strip())
        return "; ".join(lines)


def register(scope: McpScope, cwd: Path, adding: bool, port: int) -> str | Failure:
    removed = claude_mcp(["remove", "--scope", scope, NAME], cwd)
    if isinstance(removed, Failure):
        return removed
    elif not adding and removed.returncode == 0:
        return f"removed lain's MCP server from Claude Code's {scope} scope"
    elif not adding:
        return f"lain's MCP server was not registered in Claude Code's {scope} scope"
    else:
        added = claude_mcp(["add", "--scope", scope, "--transport", "http", NAME, endpoint(scope, cwd, port)], cwd)
        if isinstance(added, Failure):
            return added
        elif added.returncode != 0:
            return Failure(f"claude mcp add failed: {(added.stderr or added.stdout).strip()}")
        else:
            return f"registered lain's MCP server in Claude Code's {scope} scope"
