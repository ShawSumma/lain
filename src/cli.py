from __future__ import annotations

import difflib
import json
import sys
import textwrap
import time
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from urllib.parse import quote

from build import changed_since, current
from config import SETTINGS, address
from daemon import LOG, USER_SETTINGS, LiveEvent, Scope, claude_install, ensure, forward, rebuilt, serve, service_state, whoami
from hook import handlers_in
from jobs import NAMES, StatusData, headline
from mcp import registration
from model import Failure
from places import CHECKOUT, INSTALL, in_checkout, repo_of
from rpc import JobsData, PageData, Reply, asked, span_line
from store import JSON

type Shape = Literal["one", "maybe", "words", "list"]
type Takes = Literal["flag", "value"]

FOLLOW_S = 3.0
WIDTH = 100
INDENT = 34
JOBS = ", ".join(NAMES)


@dataclass(frozen=True)
class Arg:
    name: str
    key: str
    shape: Shape
    about: str


@dataclass(frozen=True)
class Option:
    flag: str
    key: str
    takes: Takes
    name: str


@dataclass(frozen=True)
class Command:
    words: tuple[str, ...]
    method: str
    args: tuple[Arg, ...]
    options: tuple[Option, ...]
    about: str


@dataclass(frozen=True)
class Parsed:
    command: Command
    values: dict[str, JSON]
    directory: Path
    port: int


@dataclass(frozen=True)
class Global:
    rest: list[str]
    directory: Path
    port: int


PATH = Arg("PATH", "path", "one", "the file, relative to the project root")
MAYBE_PATH = Arg("PATH", "path", "maybe", "the file or directory, relative to the project root")
MEMORY_ID = Arg("ID", "id", "one", "the memory's number, as recall and the leads show it, such as 42")
TEXT = Arg("TEXT", "text", "one", "the text, quoted as one argument")
JOB_LIST = Arg("JOB...", "jobs", "list", f"which jobs: {JOBS}")
USER = Option("--user", "user", "flag", "")
HELP = Command(("help",), "help", (Arg("COMMAND...", "command", "words", "the command"),), (), "every command, or one in full")

GROUPS: tuple[tuple[str, tuple[Command, ...]], ...] = (
    (
        "What lain remembers",
        (
            Command(("recall",), "recall", (Arg("WORDS...", "query", "words", "the words to look for"),), (), "find the stories, memories and files that match some words, and the places in lain's map"),
            Command(("show", "memory"), "show_memory", (PATH,), (), "what lain remembers about one file"),
            Command(("show", "map"), "show_map", (MAYBE_PATH,), (), "lain's map of a file or directory, or of the whole project"),
            Command(("show", "story"), "show_story", (Arg("REF", "ref", "one", "the story's ref, such as sessions/2026-10-29-051203/17, from lain list stories"),), (), "one story: the request, its rules, decisions, what was done and learned"),
            Command(("list", "stories"), "list_stories", (), (), "every story, newest first"),
            Command(("list", "decisions"), "list_decisions", (Arg("PATH", "path", "maybe", "the file, relative to the project root"),), (), "the decisions that hold, for the project or one file"),
            Command(
                ("remember",),
                "remember",
                (Arg("TEXT", "text", "one", "the fact or decision, quoted as one argument"),),
                (Option("--decision", "decision", "flag", ""), Option("--about", "files", "value", "FILE")),
                "record a fact about the project, or with --decision a decision, as stated; --about once per file it is about",
            ),
            Command(("forget",), "forget", (MEMORY_ID, Arg("REASON", "reason", "maybe", "why it no longer holds")), (), "retract a memory, on your word"),
            Command(("correct",), "correct", (MEMORY_ID, Arg("TEXT", "text", "one", "the memory as it should read, quoted as one argument")), (), "replace what a memory says, on your word"),
            Command(("pin",), "pin", (MEMORY_ID,), (), "keep a memory as it reads now, on your word"),
        ),
    ),
    (
        "Jobs, which run inside lain's process one at a time per project",
        (
            Command(("start",), "start", (JOB_LIST,), (Option("--follow", "follow", "flag", ""),), f"queue jobs, in the order given: {JOBS}; --follow says how they go until they end"),
            Command(("stop",), "stop", (JOB_LIST,), (), "stop running jobs, or take queued ones off the queue"),
            Command(("list", "jobs"), "list_jobs", (), (), "each job: running, waiting, or how it last ended"),
            Command(("show", "job"), "show_job", (Arg("JOB", "job", "one", f"the job: {JOBS}"),), (), "how one job is going or how it ended, with the end of its output"),
        ),
    ),
    (
        "Letters between sessions, agents and programs",
        (
            Command(("list", "addresses"), "list_addresses", (), (), "who can be sent a letter now"),
            Command(
                ("send", "letter"),
                "send_letter",
                (Arg("TO", "to", "one", "the address: #N, a name, or NAME.* for everyone under it"), Arg("TEXT", "text", "one", "the letter, quoted as one argument")),
                (Option("--from", "sender", "value", "ADDRESS"),),
                "send a letter, from PROJECT.user unless --from gives another address",
            ),
            Command(("read", "letters"), "read_letters", (Arg("ADDRESS", "address", "one", "the address to read for"),), (), "take the unread letters for an address"),
        ),
    ),
    (
        "lain itself",
        (
            Command(("status",), "status", (), (), "lain's process, its build, the projects it serves, its calls, and how it is put into Claude Code"),
            Command(("list", "calls"), "list_calls", (), (), "the model calls running now, and the last hour's, by API"),
            Command(("show", "page"), "show_page", (), (), "the address of lain's page for this project"),
            Command(("open", "page"), "open_page", (), (), "open lain's page for this project in the browser"),
            Command(("watch",), "watch", (), (Option("--all", "all", "flag", ""),), "every span as it starts and ends, for this project, or with --all for every one"),
            Command(("list", "bugs"), "list_bugs", (), (), "the bugs lain has recorded in itself, newest first"),
            Command(("show", "bug"), "show_bug", (Arg("NUMBER", "id", "one", "the bug's number, from lain list bugs or the error that named it"),), (), "one bug in full"),
            Command(("report", "bug"), "report_bug", (Arg("TEXT", "text", "one", "what went wrong, quoted as one argument"),), (), "record a bug in lain itself, to be fixed later"),
            Command(("restart",), "restart", (), (), "build lain from its checkout and move its process onto that build; if it does not start, the build before runs again"),
            Command(("quit",), "quit", (), (), "stop lain's process; the next command or session starts it again, unless it is a systemd service, which systemd restarts"),
            Command(("install",), "install", (), (USER,), "put lain into Claude Code for this project, or with --user for every project, with lain's process as a systemd user service"),
            Command(("uninstall",), "uninstall", (), (USER,), "take lain out of Claude Code, for this project or with --user for every project"),
            Command(("serve",), "serve", (), (), "run lain's process in the foreground"),
            Command(("hook",), "hook", (), (), "hand a Claude Code hook on standard input to lain's process; the SessionStart hook runs this"),
            HELP,
        ),
    ),
)


def every() -> list[Command]:
    found: list[Command] = []
    for _, commands in GROUPS:
        found.extend(commands)
    return found


def phrase(command: Command) -> str:
    return f"lain {' '.join(command.words)}"


def usage(command: Command) -> str:
    parts = [phrase(command)]
    for arg in command.args:
        match arg.shape:
            case "maybe":
                parts.append(f"[{arg.name}]")
            case "words" if command.method == "help":
                parts.append(f"[{arg.name}]")
            case _:
                parts.append(arg.name)
    for option in command.options:
        if option.takes == "flag":
            parts.append(f"[{option.flag}]")
        else:
            parts.append(f"[{option.flag} {option.name}]")
    return " ".join(parts)


def described(command: Command) -> list[str]:
    shown = usage(command).removeprefix("lain ")
    about = textwrap.wrap(command.about, WIDTH - INDENT)
    if len(shown) > INDENT - 4:
        lines = [f"  {shown}"]
        for line in about:
            lines.append(f"{' ' * INDENT}{line}")
        return lines
    else:
        lines = [f"  {shown:<{INDENT - 2}}{about[0]}"]
        for line in about[1:]:
            lines.append(f"{' ' * INDENT}{line}")
        return lines


def help_text() -> str:
    lines = [
        "lain keeps what work on a project decided and learned, maps the project, and runs the jobs that read history in.",
        "",
        "usage: lain [-C DIRECTORY] [--port PORT] COMMAND ...",
        "  -C DIRECTORY    the project, any directory in its git checkout; the current directory if not given",
        f"  --port PORT     the port of lain's process; {SETTINGS.port} if not given",
        "",
        "A command is a verb and what it acts on, and a Claude Code session has the same call as lain's MCP tool",
        "of that name: lain show story is show_story, lain recall is recall.",
    ]
    for title, commands in GROUPS:
        lines.extend(["", f"{title}:"])
        for command in commands:
            lines.extend(described(command))
    return "\n".join(lines)


def command_help(command: Command) -> str:
    lines = [f"usage: {usage(command)}", "", textwrap.fill(command.about, WIDTH)]
    for arg in command.args:
        lines.append(f"  {arg.name:<12} {arg.about}")
    for option in command.options:
        named = option.flag
        if option.takes == "value":
            named = f"{option.flag} {option.name}"
        lines.append(f"  {named:<12} {option_about(command, option)}")
    return "\n".join(lines)


def option_about(command: Command, option: Option) -> str:
    match option.key:
        case "decision":
            return "record it as a decision rather than a fact"
        case "files":
            return "a file it is about, relative to the project root; give it once per file"
        case "follow":
            return "say how the jobs go until they end"
        case "sender":
            return "the address it comes from, such as a session's"
        case "all":
            return "every project, not only this one"
        case _:
            return f"for every project of yours, not only this one ({phrase(command)} without it is for this project)"


def globals_of(argv: list[str]) -> Global | str:
    rest: list[str] = []
    directory = Path(".")
    port = SETTINGS.port
    index = 0
    while index < len(argv):
        word = argv[index]
        if word in ("-C", "--port") and index + 1 >= len(argv):
            return f"lain: {word} needs a value after it"
        elif word == "-C":
            directory = Path(argv[index + 1])
            index += 2
        elif word == "--port" and not argv[index + 1].isdigit():
            return f"lain: --port needs a number, not {argv[index + 1]!r}"
        elif word == "--port":
            port = int(argv[index + 1])
            index += 2
        else:
            rest.append(word)
            index += 1
    return Global(rest, directory, port)


def matched(rest: list[str]) -> Command | None:
    best: Command | None = None
    for command in every():
        size = len(command.words)
        if tuple(rest[:size]) == command.words and (best is None or size > len(best.words)):
            best = command
    return best


def unknown(rest: list[str]) -> str:
    verb = rest[0]
    objects: list[Command] = []
    for command in every():
        if command.words[0] == verb and len(command.words) > 1:
            objects.append(command)
    if len(objects) > 0:
        lines = [f"lain {verb}: {verb} what? One of:"]
        for command in objects:
            lines.append(f"  {usage(command)}")
        return "\n".join(lines)
    else:
        phrases: list[str] = []
        for command in every():
            phrases.append(" ".join(command.words))
            phrases.append(command.words[-1])
        close: list[str] = []
        for each in difflib.get_close_matches(verb, phrases, 6, 0.6):
            for command in every():
                if each in (" ".join(command.words), command.words[-1]) and " ".join(command.words) not in close:
                    close.append(" ".join(command.words))
        lines = [f"lain has no command {verb!r}."]
        if len(close) > 0:
            lines.append(f"Did you mean: lain {', lain '.join(close)}?")
        lines.append("lain help lists every command.")
        return "\n".join(lines)


def defaults(command: Command) -> dict[str, JSON]:
    values: dict[str, JSON] = {}
    for arg in command.args:
        match arg.shape:
            case "list":
                values[arg.key] = []
            case _:
                values[arg.key] = ""
    for option in command.options:
        match option.takes:
            case "flag":
                values[option.key] = False
            case _ if option.key == "files":
                values[option.key] = []
            case _:
                values[option.key] = ""
    return values


def option_of(command: Command, flag: str) -> Option | None:
    for option in command.options:
        if option.flag == flag:
            return option
    return None


def wrong(command: Command, said: str) -> str:
    return f"{phrase(command)}: {said}\nusage: {usage(command)}"


def taken(values: dict[str, JSON], option: Option, given: str) -> None:
    held = values[option.key]
    if isinstance(held, list):
        held.append(given)
    else:
        values[option.key] = given


def filled(command: Command, tokens: list[str]) -> dict[str, JSON] | str:
    values = defaults(command)
    positional: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        option = option_of(command, token)
        if token.startswith("--") and option is None:
            flags: list[str] = []
            for each in command.options:
                flags.append(each.flag)
            others = "it takes no options"
            if len(flags) > 0:
                others = f"it takes {', '.join(flags)}"
            return wrong(command, f"there is no option {token}; {others}")
        elif option is not None and option.takes == "flag":
            values[option.key] = True
            index += 1
        elif option is not None and index + 1 >= len(tokens):
            return wrong(command, f"{option.flag} needs {option.name} after it")
        elif option is not None:
            taken(values, option, tokens[index + 1])
            index += 2
        else:
            positional.append(token)
            index += 1
    return placed(command, values, positional)


def placed(command: Command, values: dict[str, JSON], positional: list[str]) -> dict[str, JSON] | str:
    left = list(positional)
    for arg in command.args:
        match arg.shape:
            case "one" if len(left) == 0:
                return wrong(command, f"needs {arg.name}, {arg.about}")
            case "one" | "maybe" if len(left) > 0:
                values[arg.key] = left.pop(0)
            case "words":
                values[arg.key] = " ".join(left)
                left = []
            case "list":
                listed: list[JSON] = []
                for each in left:
                    listed.append(each)
                values[arg.key] = listed
                left = []
            case _:
                pass
    if len(left) > 0:
        return wrong(command, f"does not take {' '.join(left)!r}; quote a text that has spaces")
    else:
        return values


def parsed(argv: list[str]) -> Parsed | str:
    given = globals_of(argv)
    if isinstance(given, str):
        return given
    elif len(given.rest) == 0 or given.rest[0] in ("-h", "--help"):
        return Parsed(HELP, {"command": ""}, given.directory, given.port)
    else:
        command = matched(given.rest)
        if command is None:
            return unknown(given.rest)
        else:
            tokens = given.rest[len(command.words) :]
            if "-h" in tokens or "--help" in tokens:
                return Parsed(HELP, {"command": " ".join(command.words)}, given.directory, given.port)
            else:
                values = filled(command, tokens)
                if isinstance(values, str):
                    return values
                else:
                    return Parsed(command, values, given.directory, given.port)


def project_of(cwd: Path) -> str:
    if in_checkout(cwd):
        return str(repo_of(cwd).root)
    else:
        return ""


def reply_of(port: int, method: str, project: str, params: dict[str, JSON]) -> Reply | Failure:
    reply = asked(port, method, project, params, None)
    if reply is None:
        return Failure(f"lain's process is not answering at {address(port)}; see {INSTALL / LOG}")
    elif "error" in reply:
        return Failure(reply["error"])
    else:
        return reply


def failed(command: Command, reason: str) -> int:
    print(f"{phrase(command)}: {reason}", file=sys.stderr)
    return 1


def shown(port: int, command: Command, project: str, params: dict[str, JSON]) -> int:
    reply = reply_of(port, command.method, project, params)
    if isinstance(reply, Failure):
        return failed(command, reply.reason)
    else:
        print(reply.get("text", ""))
        return 0


def watch(port: int, project: str) -> int:
    everywhere = project == ""
    with urllib.request.urlopen(f"{address(port)}/live?project={quote(project, safe='')}&since=0", timeout=None) as stream:
        for raw in stream:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if line.startswith("data: "):
                event: LiveEvent = json.loads(line.removeprefix("data: "))
                if "span" in event:
                    print(span_line(event["span"], everywhere), flush=True)
    return 0


def states(port: int, project: str, names: list[str]) -> list[StatusData] | Failure:
    reply = reply_of(port, "list_jobs", project, {})
    if isinstance(reply, Failure):
        return reply
    else:
        found: list[StatusData] = []
        for each in cast(JobsData, reply.get("result", {"jobs": []}))["jobs"]:
            if each["name"] in names:
                found.append(each)
        return found


def follow(port: int, command: Command, project: str, names: list[str]) -> int:
    said: dict[str, str] = {}
    while True:
        found = states(port, project, names)
        if isinstance(found, Failure):
            return failed(command, found.reason)
        else:
            going = False
            ok = True
            for each in found:
                line = headline(each)
                if said.get(each["name"]) != line:
                    print(f"{time.strftime('%H:%M:%S')} {line}", flush=True)
                    said[each["name"]] = line
                going = going or each["state"] in ("running", "waiting")
                ok = ok and each["state"] == "finished"
            if not going and ok:
                return 0
            elif not going:
                return 1
            else:
                time.sleep(FOLLOW_S)


def opened(port: int, command: Command, project: str) -> int:
    reply = reply_of(port, "show_page", project, {})
    if isinstance(reply, Failure):
        return failed(command, reply.reason)
    else:
        page = cast(PageData, reply.get("result", {"address": address(port)}))["address"]
        print(page)
        webbrowser.open(page)
        return 0


def hooks_line(where: str, path: Path, port: int) -> str:
    found = handlers_in(path, port)
    if len(found) == 0:
        return f"hooks {where}: none, in {path}"
    else:
        return f"hooks {where}: {len(found)}, in {path}"


def build_line(port: int) -> str:
    made = current()
    who = whoami(port)
    if made is None:
        return "build: none yet"
    elif changed_since(made):
        return f"build: {made.name}; {CHECKOUT} has changed since, and lain restart builds it and moves lain onto it"
    elif who is not None and who["build"] != made.name:
        return f"build: {made.name}, but lain's process still runs {who['build']}; lain restart moves it"
    else:
        return f"build: {made.name}, the checkout as it is now"


def status(port: int, command: Command, cwd: Path) -> int:
    code = shown(port, command, "", {})
    root = repo_of(cwd).root
    print(build_line(port))
    print(hooks_line("for every project", USER_SETTINGS, port))
    if in_checkout(cwd):
        print(hooks_line(f"for {root}", root / ".claude" / "settings.local.json", port))
    print(f"MCP server here: {registration(cwd)}")
    print(f"lain's process: {service_state()}")
    return code


def scope_of(user: bool) -> Scope:
    if user:
        return "user"
    else:
        return "project"


def putting(port: int, command: Command, cwd: Path, user: bool) -> int:
    if not user and not in_checkout(cwd):
        return failed(command, "this is not in a git checkout, and lain follows sessions only in one; --user puts lain in for every project")
    else:
        return claude_install(repo_of(cwd).root, scope_of(user), command.method == "install", port)


def restarted(port: int, command: Command) -> int:
    done = rebuilt(port)
    if isinstance(done, Failure):
        return failed(command, done.reason)
    else:
        print(done)
        return 0


def helped(given: str) -> int:
    if given == "":
        print(help_text())
        return 0
    else:
        command = matched(given.split())
        if command is None:
            print(unknown(given.split()), file=sys.stderr)
            return 1
        else:
            print(command_help(command))
            return 0


def warned(port: int) -> None:
    made = current()
    who = whoami(port)
    if made is not None and who is not None and who["build"] != made.name:
        print(f"lain's process runs build {who['build']}, not the current build {made.name}; lain restart moves it", file=sys.stderr)


def params_of(found: Parsed) -> dict[str, JSON]:
    values = dict(found.values)
    if found.command.method == "send_letter" and values.get("sender") == "":
        values["sender"] = f"{repo_of(found.directory.resolve()).name.lower()}.user"
    if found.command.method == "start":
        values.pop("follow", None)
    return values


def used(found: Parsed) -> int:
    command = found.command
    cwd = found.directory.resolve()
    port = found.port
    project = project_of(cwd)
    match command.method:
        case "help":
            return helped(str(found.values.get("command", "")))
        case "serve":
            return serve(SETTINGS, port, INSTALL)
        case "hook":
            return forward(port)
        case "install" | "uninstall":
            return putting(port, command, cwd, found.values.get("user") is True)
        case "restart":
            return restarted(port, command)
        case "quit":
            return shown(port, command, "", {})
        case _ if not ensure(port):
            return failed(command, f"lain's process did not start; see {INSTALL / LOG}")
        case _:
            warned(port)
            return asked_for(found, command, port, cwd, project)


def asked_for(found: Parsed, command: Command, port: int, cwd: Path, project: str) -> int:
    match command.method:
        case "status":
            return status(port, command, cwd)
        case "watch" if found.values.get("all") is True:
            return watch(port, "")
        case "watch":
            return watch(port, project)
        case "open_page":
            return opened(port, command, project)
        case "start" if found.values.get("follow") is True:
            code = shown(port, command, project, params_of(found))
            if code == 0:
                names: list[str] = []
                for each in cast(list[str], found.values["jobs"]):
                    names.append(each)
                return follow(port, command, project, names)
            else:
                return code
        case _:
            return shown(port, command, project, params_of(found))


def main(argv: list[str]) -> int:
    found = parsed(argv)
    if isinstance(found, str):
        print(found, file=sys.stderr)
        return 2
    else:
        return used(found)


def entry() -> int:
    try:
        return main(sys.argv[1:])
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(entry())
