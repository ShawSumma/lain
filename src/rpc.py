from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, TypedDict, cast
from urllib.parse import quote

from bugs import BugsData, ReportedData, known, known_line, known_lines, noticed, reported
from config import SETTINGS, address
from extract import Stretch, known_files
from hand import corrected, forgotten
from history import History, Note
from ingest import live_session, session_homes
from jobs import NAMES, Job, Jobs, StatusData, described, headline, job_named, table
from memory import NONE, MemoryKind, Store, lead, standing
from model import Failure
from places import NOTES, Places
from post import LetterData, Office, address_of, shown
from prompts import message
from spans import ChunkData
from spec import Field, Tool, fields_wrong
from stale import staleness
from store import JSON, locked, record_of
from story import STORIES, EntryData, FileData, ItemData, Library, RegionData, SearchData, StoryData, file_markdown, hit_lines, item_of, region_markdown, story_markdown

type SpanState = Literal["running", "ok", "failed", "lost"]


class NodeData(TypedDict):
    id: str
    parent: str | None
    kind: str
    name: str
    project: str
    pid: int
    started: float
    ended: float | None
    state: SpanState
    attrs: dict[str, JSON]
    stream: str
    children: int
    running: int
    depth: int


class RootsData(TypedDict):
    seq: int
    spans: list[NodeData]


class OpenedData(TypedDict):
    span: NodeData
    children: list[NodeData]
    chunks: list[ChunkData]


class FlightData(TypedDict):
    id: str
    api: str
    model: str
    purpose: str
    subject: str
    project: str
    started: float


class TallyData(TypedDict):
    running: int
    done: int
    failed: int
    prompt: int
    cached: int
    completion: int
    seconds: float
    busy: float


class FlightsData(TypedDict):
    running: list[FlightData]
    apis: dict[str, TallyData]


class ProjectData(TypedDict):
    root: str
    name: str
    running: list[str]


class WhoData(TypedDict):
    lain: str
    build: str
    pid: int
    port: int
    started: float


class DaemonData(TypedDict):
    who: WhoData
    projects: list[ProjectData]
    flight: FlightsData


class RecalledData(TypedDict):
    memory: SearchData
    map: SearchData


class StoriesData(TypedDict):
    stories: list[EntryData]


class HeldData(TypedDict):
    item: ItemData
    notes: list[str]


class DecisionsData(TypedDict):
    decisions: list[HeldData]


class NotedData(TypedDict):
    id: str
    files: list[str]
    unknown: list[str]


class AddressData(TypedDict):
    number: int
    name: str


class AddressesData(TypedDict):
    addresses: list[AddressData]


class LettersData(TypedDict):
    letters: list[LetterData]


class BegunData(TypedDict):
    job: Job
    started: float
    fresh: bool


class StartedData(TypedDict):
    jobs: list[BegunData]


class StoppedData(TypedDict):
    job: Job
    was: Literal["stopping", "dropped", "idle"]


class StopsData(TypedDict):
    jobs: list[StoppedData]


class JobsData(TypedDict):
    jobs: list[StatusData]


class PageData(TypedDict):
    address: str


class QuitData(TypedDict):
    pid: int


class PathArgs(TypedDict):
    path: str


class QueryArgs(TypedDict):
    query: str


class RefArgs(TypedDict):
    ref: str


class RememberArgs(TypedDict):
    text: str
    decision: bool
    files: list[str]


class IdArgs(TypedDict):
    id: str


class ForgetArgs(TypedDict):
    id: str
    reason: str


class CorrectArgs(TypedDict):
    id: str
    text: str


class SendArgs(TypedDict):
    sender: str
    to: str
    text: str


class AddressArgs(TypedDict):
    address: str


class JobsArgs(TypedDict):
    jobs: list[str]


class JobArgs(TypedDict):
    job: Job


class SpansArgs(TypedDict):
    project: str
    before: float
    limit: int


class BugArgs(TypedDict):
    text: str


type Answer = (
    WhoData
    | DaemonData
    | FlightsData
    | RootsData
    | OpenedData
    | QuitData
    | FileData
    | RecalledData
    | StoriesData
    | StoryData
    | DecisionsData
    | RegionData
    | NotedData
    | AddressesData
    | LettersData
    | StartedData
    | StopsData
    | JobsData
    | StatusData
    | PageData
    | ReportedData
    | BugsData
)


class Request(TypedDict):
    method: str
    project: str
    params: dict[str, JSON]


class Reply(TypedDict, total=False):
    result: Answer
    text: str
    error: str


@dataclass(frozen=True)
class Done:
    data: Answer
    text: str


class Host(Protocol):
    port: int
    jobs: Jobs

    def who(self) -> WhoData: ...

    def projects(self) -> list[ProjectData]: ...

    def flown(self) -> FlightsData: ...

    def listed(self, project: str, before: float, limit: int) -> RootsData: ...

    def opened(self, ident: str) -> OpenedData | None: ...

    def quit(self) -> None: ...


ID = Field("id", "text")
RECALL = Tool("recall", (Field("query", "text"),))
REMEMBER = Tool("remember", (Field("text", "text"), Field("decision", "flag"), Field("files", "texts")))
FORGET = Tool("forget", (ID, Field("reason", "text")))
CORRECT = Tool("correct", (ID, Field("text", "text")))
PIN = Tool("pin", (ID,))
SHOW_MEMORY = Tool("show_memory", (Field("path", "text"),))
SHOW_MAP = Tool("show_map", (Field("path", "text"),))
SHOW_STORY = Tool("show_story", (Field("ref", "text"),))
LIST_STORIES = Tool("list_stories", ())
LIST_DECISIONS = Tool("list_decisions", (Field("path", "text"),))
START = Tool("start", (Field("jobs", "texts"),))
STOP = Tool("stop", (Field("jobs", "texts", owner="start"),))
LIST_JOBS = Tool("list_jobs", ())
SHOW_JOB = Tool("show_job", (Field("job", "text", choices=NAMES),))
LIST_ADDRESSES = Tool("list_addresses", ())
SEND_LETTER = Tool("send_letter", (Field("sender", "text"), Field("to", "text", owner="send"), Field("text", "text", owner="send")))
READ_LETTERS = Tool("read_letters", (Field("address", "text"),))
SHOW_PAGE = Tool("show_page", ())
REPORT_BUG = Tool("report_bug", (Field("text", "text"),))
LIST_BUGS = Tool("list_bugs", ())
SHOW_BUG = Tool("show_bug", (ID,))
OFFERED = (
    RECALL,
    REMEMBER,
    SHOW_MEMORY,
    SHOW_MAP,
    SHOW_STORY,
    LIST_STORIES,
    LIST_DECISIONS,
    START,
    STOP,
    LIST_JOBS,
    SHOW_JOB,
    LIST_ADDRESSES,
    SEND_LETTER,
    READ_LETTERS,
    SHOW_PAGE,
    REPORT_BUG,
    LIST_BUGS,
    SHOW_BUG,
)
BY_HAND = (FORGET, CORRECT, PIN)
DAEMON = (
    Tool("whoami", ()),
    Tool("status", ()),
    Tool("list_calls", ()),
    Tool("list_spans", (Field("project", "text"), Field("before", "real"), Field("limit", "number"))),
    Tool("show_span", (ID,)),
    LIST_BUGS,
    SHOW_BUG,
    Tool("quit", ()),
)


def clock(stamp: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(stamp))


def span_line(node: NodeData, everywhere: bool) -> str:
    where = ""
    if everywhere and node["project"] != "":
        where = f"{Path(node['project']).name} "
    took = ""
    if node["ended"] is not None:
        took = f" after {node['ended'] - node['started']:.1f}s"
    return f"{clock(node['ended'] or node['started'])} {where}{'  ' * node['depth']}{node['kind']} {node['name']}: {node['state']}{took}"


def flight_lines(found: FlightsData) -> list[str]:
    lines: list[str] = []
    if len(found["apis"]) == 0:
        lines.append("no model calls in the last hour")
    for api, tally in sorted(found["apis"].items()):
        lines.append(
            f"{api}: {tally['running']} running; in the last hour {tally['done']} done, {tally['failed']} failed, "
            f"{tally['prompt']} tokens in ({tally['cached']} read from the cache), {tally['completion']} out, "
            f"{tally['seconds']:.0f}s of calls, some call in flight for {tally['busy']:.0f}s"
        )
    for each in found["running"]:
        lines.append(f"  {each['api']} {each['model']} {each['purpose']} {each['subject']} for {Path(each['project']).name}, {time.time() - each['started']:.0f}s so far")
    return lines


def daemon_lines(found: DaemonData) -> list[str]:
    who = found["who"]
    lines = [f"lain's process {who['pid']}, build {who['build']} of {who['lain']}, serving {address(who['port'])} for {time.time() - who['started']:.0f}s"]
    for each in found["projects"]:
        running = ""
        if len(each["running"]) > 0:
            running = f": {', '.join(each['running'])} queued or running"
        lines.append(f"  {each['root']}{running}")
    lines.extend(flight_lines(found["flight"]))
    return lines


def story_lines(entries: list[EntryData]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        lines.append(f"{entry['ref']} {entry['title']} ({entry['changed']} files changed, {entry['remembered']} remembered)")
    return lines


def letter_lines(office: Office, letters: list[LetterData]) -> list[str]:
    numbers: dict[str, int] = {}
    for each in office.names():
        numbers[each.name] = each.number
    lines: list[str] = []
    for letter in letters:
        lines.append(shown(letter, numbers))
    return lines


def tool_of(name: str) -> Tool | None:
    for tool in (*OFFERED, *BY_HAND, *DAEMON):
        if tool.name == name:
            return tool
    return None


def called(host: Host, found: Places | None, name: str, params: dict[str, JSON]) -> Done | Failure:
    done = dispatched(host, found, name, params)
    if isinstance(done, Done) and len(done.text) > SETTINGS.result_room:
        project = ""
        if found is not None:
            project = str(found.repo.root)
        noticed("rpc", project, f"{name} answered with more than {SETTINGS.result_room} characters", f"{name} {json.dumps(params)[:400]}: {len(done.text)} characters")
        kept = done.text[: SETTINGS.result_room].rpartition("\n")[0]
        return Done(done.data, f"{kept}\n... {len(done.text) - len(kept)} more characters left out; ask for less")
    else:
        return done


def dispatched(host: Host, found: Places | None, name: str, params: dict[str, JSON]) -> Done | Failure:
    tool = tool_of(name)
    wrong: str | None = None
    if tool is not None:
        wrong = fields_wrong(tool.fields, params, "params")
    if tool is None:
        return Failure(f"lain has no method {name}")
    elif wrong is not None:
        return Failure(wrong)
    elif tool in DAEMON:
        return daemon_called(host, name, params)
    elif found is None:
        return Failure(f"{name} is about a project; name a directory in a git checkout")
    else:
        return project_called(host, found, name, params)


def daemon_called(host: Host, name: str, params: dict[str, JSON]) -> Done | Failure:
    match name:
        case "whoami":
            who = host.who()
            return Done(who, f"lain's process {who['pid']}, build {who['build']}")
        case "status":
            state: DaemonData = {"who": host.who(), "projects": host.projects(), "flight": host.flown()}
            return Done(state, "\n".join(daemon_lines(state)))
        case "list_calls":
            flight = host.flown()
            return Done(flight, "\n".join(flight_lines(flight)))
        case "list_spans":
            asked = cast(SpansArgs, params)
            roots = host.listed(asked["project"], asked["before"], asked["limit"])
            lines: list[str] = []
            for node in roots["spans"]:
                lines.append(span_line(node, asked["project"] == ""))
            return Done(roots, "\n".join(lines))
        case "show_span":
            ident = cast(IdArgs, params)["id"]
            opened = host.opened(ident)
            if opened is None:
                return Failure(f"lain has no span {ident}")
            else:
                return Done(opened, span_line(opened["span"], True))
        case "list_bugs":
            found = known()
            listed: BugsData = {"bugs": found}
            return Done(listed, "\n".join(known_lines(found)))
        case "show_bug":
            return bug_of(cast(IdArgs, params)["id"])
        case _:
            host.quit()
            gone: QuitData = {"pid": host.who()["pid"]}
            return Done(gone, "lain is stopping")


def bug_of(ident: str) -> Done | Failure:
    for each in known():
        if str(each["number"]) == ident.removeprefix("bug ").strip():
            return Done({"bugs": [each]}, f"{known_line(each)}\n\n{each['detail']}")
    return Failure(f"lain has recorded no bug {ident}; list_bugs lists them")


def bug_reported(found: Places, text: str) -> Done | Failure:
    if text.strip() == "":
        return Failure("report_bug needs what went wrong")
    else:
        kept: ReportedData = {"number": reported(text.strip(), str(found.repo.root))}
        return Done(kept, f"recorded as lain bug {kept['number']}")


def library(found: Places) -> Library:
    return Library(found, Store(found.memory))


def project_called(host: Host, found: Places, name: str, params: dict[str, JSON]) -> Done | Failure:
    match name:
        case "recall":
            return recalled(library(found), cast(QueryArgs, params)["query"])
        case "remember":
            return remembered(found, cast(RememberArgs, params))
        case "forget" | "correct" | "pin":
            return handled(found, name, params)
        case "show_memory":
            page = library(found).file(cast(PathArgs, params)["path"])
            return Done(page, file_markdown(page))
        case "show_map":
            region = library(found).region(cast(PathArgs, params)["path"] or ".")
            return Done(region, region_markdown(region))
        case "show_story":
            return story_of(library(found), cast(RefArgs, params)["ref"])
        case "list_stories":
            every = library(found).entries()
            listed: StoriesData = {"stories": every[:STORIES]}
            lines = story_lines(listed["stories"])
            if len(every) > STORIES:
                lines.append(f"{len(every) - STORIES} older stories are left out; recall finds them by words")
            return Done(listed, "\n".join(lines) or "no stories yet")
        case "list_decisions":
            return decisions(found, cast(PathArgs, params)["path"])
        case "start" | "stop" | "list_jobs" | "show_job":
            return jobbed(host.jobs, found, name, params)
        case "list_addresses" | "send_letter" | "read_letters":
            return lettered(found, name, params)
        case "report_bug":
            return bug_reported(found, cast(BugArgs, params)["text"])
        case _:
            page_at: PageData = {"address": f"{address(host.port)}/?project={quote(str(found.repo.root), safe='')}"}
            return Done(page_at, message("page_open", address=page_at["address"]))


def recalled(books: Library, query: str) -> Done:
    hits = books.search(query)
    places = books.where(query)
    lines = hit_lines(hits)
    found = hit_lines(places)
    if len(found) > 0:
        lines.extend(["", "In lain's map:", *found])
    kept: RecalledData = {"memory": hits, "map": places}
    return Done(kept, "\n".join(lines))


def story_of(books: Library, ref: str) -> Done | Failure:
    stretch = books.stretch(ref)
    commit = books.commit_story(ref)
    if stretch is not None:
        told = books.story(stretch)
        return Done(told, story_markdown(told))
    elif commit is not None:
        return Done(commit, story_markdown(commit))
    else:
        return Failure(f"there is no story {ref}; list_stories lists them")


def decisions(found: Places, path: str) -> Done:
    store = Store(found.memory)
    files: list[str] | None = None
    if path != "":
        files = [path]
    watching = staleness(found, store)
    held: DecisionsData = {"decisions": []}
    lines: list[str] = []
    for memory in sorted(store.relevant(files, watching), key=standing):
        if memory.kind == "decision":
            notes = watching.notes(memory)
            held["decisions"].append({"item": item_of(memory), "notes": notes})
            lines.append(lead(memory, notes))
    if len(lines) == 0:
        return Done(held, message("no_leads"))
    else:
        return Done(held, "\n".join([message("lead_bases"), *lines]))


def remembered(found: Places, args: RememberArgs) -> Done | Failure:
    text = args["text"].strip()
    if text == "":
        return Failure("remember needs the text of the decision or fact")
    else:
        files = known_files(Stretch(NOTES, ()), found.repo.root, args["files"])
        with locked(found.agent(NOTES)):
            step = History(found.agent(NOTES)).append(Note(text))
        kind: MemoryKind = "observation"
        if args["decision"]:
            kind = "decision"
        store = Store(found.memory)
        memory = store.add(kind, text, "stated", f"{NOTES}/{step.number}", step.time, files, (), (), (text,), NONE)
        unknown: list[str] = []
        for path in args["files"]:
            if path not in files:
                unknown.append(path)
        kept: NotedData = {"id": memory.id, "files": list(files), "unknown": unknown}
        about = ", ".join(files) or "no file"
        if len(unknown) > 0:
            about = f"{about}; not in the project: {', '.join(unknown)}"
        return Done(kept, f"kept {kind} {memory.id}, stated, about {about}")


def handled(found: Places, name: str, params: dict[str, JSON]) -> Done | Failure:
    store = Store(found.memory)
    ident = cast(IdArgs, params)["id"]
    target = store.nodes.get(ident)
    if target is None:
        return Failure(f"lain remembers nothing with the id {ident}; recall finds memories by words")
    else:
        match name:
            case "forget":
                made = forgotten(found, store, target, cast(ForgetArgs, params)["reason"])
                return Done({"id": made.id, "files": [], "unknown": []}, f"forgot {ident}, as the user's")
            case "correct":
                made = corrected(found, store, target, cast(CorrectArgs, params)["text"])
                return Done({"id": made.id, "files": [], "unknown": []}, f"corrected {ident} as {made.id}, the user's")
            case _:
                made = corrected(found, store, target, None)
                return Done({"id": made.id, "files": [], "unknown": []}, f"pinned {ident} as {made.id}, the user's")


def live_names(found: Places, office: Office) -> list[AddressData]:
    sessions: dict[str, str] = {}
    for session in session_homes(found):
        sessions[address_of(found.repo.name, session)] = session
    kept: list[AddressData] = []
    for each in office.names():
        session = sessions.get(each.name)
        if session is None or live_session(found.agent(session)):
            kept.append({"number": each.number, "name": each.name})
    return kept


def lettered(found: Places, name: str, params: dict[str, JSON]) -> Done | Failure:
    office = Office(found.home)
    match name:
        case "list_addresses":
            listed: AddressesData = {"addresses": live_names(found, office)}
            lines: list[str] = []
            for each in listed["addresses"]:
                lines.append(f"#{each['number']} {each['name']}")
            return Done(listed, "\n".join(lines) or "no one has an address now")
        case "send_letter":
            asked = cast(SendArgs, params)
            if asked["text"].strip() == "":
                return Failure("send_letter needs the text of the letter")
            else:
                sent = office.send(asked["sender"], asked["to"], asked["text"], None)
                if isinstance(sent, str):
                    return Failure(sent)
                else:
                    numbers: list[str] = []
                    for letter in sent:
                        numbers.append(f"{letter['number']} to {letter['to']}")
                    return Done({"letters": sent}, f"sent letter {', '.join(numbers)}")
        case _:
            name_given = cast(AddressArgs, params)["address"]
            me = office.register(name_given)
            if isinstance(me, str):
                return Failure(me)
            else:
                taken = office.take(name_given)
                return Done({"letters": taken}, "\n\n".join(letter_lines(office, taken)) or f"no unread letters for {name_given}")


def jobs_in(given: list[str]) -> list[Job] | Failure:
    found: list[Job] = []
    for text in given:
        job = job_named(text)
        if job is None:
            return Failure(f"lain has no job {text}; the jobs are {', '.join(NAMES)}")
        elif job not in found:
            found.append(job)
    if len(found) == 0:
        return Failure(f"name the jobs: {', '.join(NAMES)}")
    else:
        return found


def jobbed(jobs: Jobs, found: Places, name: str, params: dict[str, JSON]) -> Done | Failure:
    match name:
        case "start":
            asked = jobs_in(cast(JobsArgs, params)["jobs"])
            if isinstance(asked, Failure):
                return asked
            else:
                return started(jobs, found, asked)
        case "stop":
            asked = jobs_in(cast(JobsArgs, params)["jobs"])
            if isinstance(asked, Failure):
                return asked
            else:
                return stopped(jobs, found, asked)
        case "show_job":
            state = jobs.status(found, cast(JobArgs, params)["job"])
            return Done(state, described(state))
        case _:
            listed: JobsData = {"jobs": []}
            for each in NAMES:
                listed["jobs"].append(jobs.status(found, each))
            return Done(listed, table(listed["jobs"]))


def started(jobs: Jobs, found: Places, asked: list[Job]) -> Done:
    kept: StartedData = {"jobs": []}
    lines: list[str] = []
    for job in asked:
        begun = jobs.start(found, job, None)
        kept["jobs"].append({"job": job, "started": begun.time, "fresh": begun.fresh})
        said = headline(jobs.status(found, job))
        if not begun.fresh:
            said = f"{said} (already queued; not queued again)"
        lines.append(said)
    lines.append(message("job_follow"))
    return Done(kept, "\n".join(lines))


def stopped(jobs: Jobs, found: Places, asked: list[Job]) -> Done:
    kept: StopsData = {"jobs": []}
    lines: list[str] = []
    for job in asked:
        was = jobs.stop(found, job)
        kept["jobs"].append({"job": job, "was": was})
        match was:
            case "stopping":
                lines.append(f"{job}: stopping; it ends once the model calls it has in flight come back")
            case "dropped":
                lines.append(f"{job}: taken off the queue before it ran")
            case _:
                lines.append(f"{job}: was not queued or running")
    return Done(kept, "\n".join(lines))


def answered(host: Host, found: Places | None, request: Request) -> Reply:
    done = called(host, found, request["method"], request["params"])
    if isinstance(done, Failure):
        return {"error": done.reason}
    else:
        return {"result": done.data, "text": done.text}


def posted(port: int, path: str, body: bytes, timeout: float | None) -> bytes | None:
    request = urllib.request.Request(f"{address(port)}{path}", body, {"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            found: bytes = reply.read()
            return found
    except urllib.error.HTTPError as error:
        failed: bytes = error.read()
        return failed
    except (OSError, ValueError):
        return None


def asked(port: int, method: str, project: str, params: dict[str, JSON], timeout: float | None) -> Reply | None:
    request: Request = {"method": method, "project": project, "params": params}
    said = posted(port, "/rpc", json.dumps(request).encode(), timeout)
    if said is None:
        return None
    else:
        return cast(Reply | None, record_of(said.decode(errors="replace")))
