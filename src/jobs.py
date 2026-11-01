from __future__ import annotations

import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypedDict

from atlas import describe
from commits import analysed, scan
from bugs import caught
from config import Settings
from extract import Extracted, Extractor, extract_session
from hook import WORK
from ingest import TRACES, first_cwd, first_time, ingest, live_session, read_state, session_name, transcripts
from memory import Store
from model import Failure, Usage
from page import Builder, Page, dated, flag, flagged, moment, number, read, real, when, write
from places import GIT, REVIEWS, Places, places
from reviews import read_reviews
from spans import Span, chunk, span
from stale import Flagged, recheck
from store import locked
from upkeep import Pick, kept

type Job = Literal["git", "reviews", "sessions", "recheck", "map"]
type State = Literal["idle", "waiting", "running", "finished", "failed", "stopped"]
type Stopped = Literal["stopping", "dropped", "idle"]
type Say = Callable[[str], None]

NAMES: tuple[Job, ...] = ("git", "reviews", "sessions", "recheck", "map")
JOBS = "jobs"
TAIL = 15
WIDE = 400


class RunData(TypedDict):
    started: float
    ended: float
    stopped: bool
    failures: int
    made: dict[str, int]
    tail: list[str]


class StatusData(TypedDict):
    name: Job
    state: State
    started: float | None
    ended: float | None
    failures: int | None
    progress: str
    after: list[str]
    upkeep: bool
    made: dict[str, int]
    tail: list[str]


@dataclass(frozen=True)
class Begun:
    time: float
    fresh: bool


@dataclass
class Running:
    name: Job
    started: float
    upkeep: Pick | None
    began: float | None = None
    before: dict[str, int] = field(default_factory=dict)
    stop: threading.Event = field(default_factory=threading.Event)
    said: deque[str] = field(default_factory=lambda: deque(maxlen=TAIL))
    failures: int = 0


@dataclass(frozen=True)
class Found:
    places: Places
    session: str
    path: Path


def job_named(text: str) -> Job | None:
    for name in NAMES:
        if name == text:
            return name
    return None


def folder(found: Places, name: Job) -> Path:
    return found.home / JOBS / name


def counted(found: Places) -> dict[str, int]:
    kinds: Counter[str] = Counter()
    for memory in Store(found.memory).nodes.values():
        kinds[memory.kind] += 1
    return dict(kinds)


def grown(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    found: dict[str, int] = {}
    for kind, count in sorted(after.items()):
        if count > before.get(kind, 0):
            found[kind] = count - before.get(kind, 0)
    return found


def spent(usage: Usage) -> str:
    return (
        f"{usage.calls} model calls, {usage.prompt_tokens} tokens in "
        f"({usage.cached_tokens} of them from cache), {usage.completion_tokens} out, {usage.retried} model calls sent again after a failure, "
        f"{usage.seconds:.0f}s"
    )


def extracted(session: str, done: Extracted, say: Say) -> int:
    say(f"{session}: {len(done.made)} things remembered")
    for reason in done.dropped:
        say(reason)
    for reason in done.failed:
        say(f"not extracted, and tried again next time: {reason}")
    return len(done.failed)


def git_job(found: Places, settings: Settings, usage: Usage, say: Say, stop: threading.Event) -> int:
    with locked(found.agent(GIT) / WORK):
        return scan(found, settings, usage, say, stop)


def reviews_job(found: Places, settings: Settings, usage: Usage, say: Say, stop: threading.Event) -> int:
    taken = read_reviews(found, settings.pr_batch)
    failures = 0
    if isinstance(taken, Failure):
        say(f"stopped reading pull requests, and carries on from there next time: {taken.reason}")
        failures = 1
    else:
        say(f"reviews: read in {taken} closed pull requests")
    with locked(found.agent(REVIEWS) / WORK):
        store = Store(found.memory)
        done = extract_session(found, store, REVIEWS, Extractor(found, settings, usage), True, None, stop)
    return failures + extracted(REVIEWS, done, say)


def sessions_of(found: Places) -> list[Found]:
    listed: list[Found] = []
    for path in transcripts(found.repo.root):
        cwd = first_cwd(path)
        if cwd is not None and cwd.is_dir():
            at = places(cwd)
            listed.append(Found(at, session_name(at, path.stem, first_time(path)), path))
    return listed


def sessions_job(found: Places, settings: Settings, usage: Usage, say: Say, stop: threading.Event) -> int:
    failures = 0
    for each in sessions_of(found):
        if not stop.is_set():
            at = each.places
            home = at.agent(each.session)
            finished = not live_session(home)
            with locked(home / WORK):
                ingest(at.home, each.session, each.path, at.repo, finished, home / TRACES)
                store = Store(at.memory)
                done = extract_session(at, store, each.session, Extractor(at, settings, usage), finished, None, stop)
            failures += extracted(each.session, done, say)
    return failures


def every(flagged: list[Flagged]) -> list[Flagged]:
    return flagged


def recheck_job(found: Places, settings: Settings, usage: Usage, say: Say, mine: Running) -> int:
    if mine.upkeep is None:
        return len(recheck(found, settings, usage, say, every, mine.stop).failed)
    else:
        say(f"upkeep after a session stopped: at most {settings.upkeep_rechecks} memories, those tried longest ago first")
        done = recheck(found, settings, usage, say, mine.upkeep, mine.stop)
        kept(found, mine.started, done)
        return len(done.failed)


def map_job(found: Places, settings: Settings, usage: Usage, say: Say, stop: threading.Event) -> int:
    done = describe(found, settings, usage, say, ".", stop)
    say(f"wrote {done.written} pages of the map, kept {done.current} whose code has not changed, and {len(done.failed)} were not written")
    return len(done.failed)


def fraction(home: Path, noun: str) -> str:
    state = read_state(home)
    if state is None:
        return ""
    else:
        return f"{len(set(state['extracted']))} of {len(state['steps'])} {noun} read in are extracted"


def counts(found: Places, name: Job) -> str:
    match name:
        case "git":
            return f"{len(analysed(found.agent(GIT)))} commits read"
        case "reviews":
            return fraction(found.agent(REVIEWS), "pull requests")
        case _:
            return ""


def run_path(found: Places, name: Job, started: float) -> Path:
    return folder(found, name) / f"{dated(started)}.md"


def run_page(name: Job, started: float, ran: RunData | None) -> Page:
    built = Builder(f"the {name} job queued at {moment(started)}")
    built.field("started", when(started))
    if ran is not None:
        built.field("ended", when(ran["ended"]))
        built.field("stopped", flag(ran["stopped"]))
        built.field("failures", str(ran["failures"]))
        for kind, count in sorted(ran["made"].items()):
            built.field("made", f"{count} {kind}")
        built.section("said", "\n".join(ran["tail"]))
    return built.page()


def run_of(path: Path) -> tuple[float, RunData | None]:
    page = read(path)
    started = real(page.need("started"))
    if page.one("ended") is None:
        return started, None
    else:
        made: dict[str, int] = {}
        for line in page.every("made"):
            count, _, kind = line.partition(" ")
            made[kind] = number(count)
        tail: list[str] = []
        said = page.value("said")
        if said != "":
            tail = said.split("\n")
        ran: RunData = {
            "started": started,
            "ended": real(page.need("ended")),
            "stopped": flagged(page.need("stopped")),
            "failures": number(page.need("failures")),
            "made": made,
            "tail": tail,
        }
        return started, ran


def runs_of(found: Places, name: Job) -> list[tuple[float, RunData | None]]:
    found_runs: list[tuple[float, RunData | None]] = []
    if folder(found, name).is_dir():
        for path in sorted(folder(found, name).glob("*.md")):
            found_runs.append(run_of(path))
    return found_runs


def ended_status(shown: StatusData, ran: RunData) -> StatusData:
    shown["ended"] = ran["ended"]
    shown["failures"] = ran["failures"]
    shown["made"] = ran["made"]
    shown["tail"] = ran["tail"]
    if ran["stopped"]:
        shown["state"] = "stopped"
    elif ran["failures"] == 0:
        shown["state"] = "finished"
    else:
        shown["state"] = "failed"
    return shown


def clock(stamp: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


def lasting(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds // 60:.0f}m"
    else:
        return f"{seconds // 3600:.0f}h{seconds % 3600 // 60:02.0f}m"


def made_text(made: dict[str, int]) -> str:
    if len(made) == 0:
        return "nothing new remembered"
    else:
        parts: list[str] = []
        for kind, count in sorted(made.items(), key=lambda pair: -pair[1]):
            parts.append(f"{count} {kind}")
        return f"remembered {', '.join(parts)}"


def when_text(shown: StatusData) -> str:
    started = shown["started"]
    ended = shown["ended"]
    match shown["state"]:
        case "idle":
            return "never run here"
        case "waiting" if len(shown["after"]) == 0:
            return "next"
        case "waiting":
            return f"after {', '.join(shown['after'])}"
        case "running" if started is not None:
            return f"for {lasting(time.time() - started)}"
        case _ if started is not None and ended is not None:
            return f"{clock(ended)}, took {lasting(ended - started)}"
        case _ if started is not None:
            return f"started {clock(started)}, ended with lain's process"
        case _:
            return ""


def what_text(shown: StatusData) -> str:
    parts: list[str] = []
    if shown["upkeep"]:
        parts.append("upkeep")
    match shown["state"]:
        case "idle" | "waiting":
            pass
        case "failed":
            parts.append(f"{shown['failures']} not taken in")
            parts.append(made_text(shown["made"]))
        case _:
            parts.append(made_text(shown["made"]))
    if shown["progress"] != "":
        parts.append(shown["progress"])
    return "; ".join(parts)


def headline(shown: StatusData) -> str:
    parts = [f"{shown['name']} {shown['state']}"]
    for part in (when_text(shown), what_text(shown)):
        if part != "":
            parts.append(part)
    return ", ".join(parts)


def table(rows: list[StatusData]) -> str:
    lines: list[str] = []
    for shown in rows:
        lines.append(f"{shown['name']:<9} {shown['state']:<9} {when_text(shown):<34} {what_text(shown)}".rstrip())
    return "\n".join(lines)


def described(shown: StatusData) -> str:
    lines = [headline(shown)]
    for line in shown["tail"]:
        lines.append(f"  {line}")
    return "\n".join(lines)


def millis(stamp: float) -> int:
    return round(stamp * 1000)


class Jobs:
    settings: Settings
    lock: threading.Lock
    queues: dict[str, list[Running]]

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.lock = threading.Lock()
        self.queues = {}

    def start(self, found: Places, name: Job, upkeep: Pick | None) -> Begun:
        key = str(found.repo.root)
        with self.lock:
            queue = self.queues.setdefault(key, [])
            for held in queue:
                if held.name == name:
                    return Begun(held.started, False)
            made = Running(name, time.time(), upkeep)
            queue.append(made)
            write(run_path(found, name, made.started), run_page(name, made.started, None))
            if len(queue) == 1:
                threading.Thread(target=self.worked, args=(found, key), daemon=True).start()
            return Begun(made.started, True)

    def worked(self, found: Places, key: str) -> None:
        going = True
        while going:
            with self.lock:
                queue = self.queues[key]
                head = queue[0]
            self.run(found, head)
            with self.lock:
                queue.remove(head)
                going = len(queue) > 0
                if not going:
                    del self.queues[key]

    def stop(self, found: Places, name: Job) -> Stopped:
        with self.lock:
            queue = self.queues.get(str(found.repo.root), [])
            for index, held in enumerate(queue):
                if held.name == name and index == 0:
                    held.stop.set()
                    return "stopping"
                elif held.name == name:
                    queue.remove(held)
                    held.stop.set()
                    held.said.append("taken off the queue before it ran")
                    self.ended(found, held)
                    return "dropped"
            return "idle"

    def run(self, found: Places, mine: Running) -> None:
        mine.before = counted(found)
        mine.began = time.time()
        try:
            with span("job", f"{mine.name} job", str(found.repo.root), None, {}) as doing:
                mine.failures = self.done(found, mine, doing)
                doing.ok = mine.failures == 0
                doing.attrs["failures"] = mine.failures
        except Exception as error:
            mine.failures += 1
            mine.said.append(f"failed: {caught(error, f'{mine.name} job', str(found.repo.root), '')}"[:WIDE])
        finally:
            self.ended(found, mine)

    def ended(self, found: Places, mine: Running) -> None:
        made: dict[str, int] = {}
        if mine.began is not None:
            made = grown(mine.before, counted(found))
        ran: RunData = {
            "started": mine.started,
            "ended": time.time(),
            "stopped": mine.stop.is_set(),
            "failures": mine.failures,
            "made": made,
            "tail": list(mine.said),
        }
        write(run_path(found, mine.name, mine.started), run_page(mine.name, mine.started, ran))

    def done(self, found: Places, mine: Running, doing: Span) -> int:
        def say(line: str) -> None:
            mine.said.append(line[:WIDE])
            chunk(doing, "note", line)

        usage = Usage()
        failures = 0
        match mine.name:
            case "git":
                failures = git_job(found, self.settings, usage, say, mine.stop)
            case "reviews":
                failures = reviews_job(found, self.settings, usage, say, mine.stop)
            case "sessions":
                failures = sessions_job(found, self.settings, usage, say, mine.stop)
            case "recheck":
                failures = recheck_job(found, self.settings, usage, say, mine)
            case "map":
                failures = map_job(found, self.settings, usage, say, mine.stop)
        say(spent(usage))
        return failures

    def status(self, found: Places, name: Job) -> StatusData:
        shown: StatusData = {
            "name": name,
            "state": "idle",
            "started": None,
            "ended": None,
            "failures": None,
            "progress": counts(found, name),
            "after": [],
            "upkeep": False,
            "made": {},
            "tail": [],
        }
        with self.lock:
            queue = list(self.queues.get(str(found.repo.root), []))
        ahead: list[str] = []
        for held in queue:
            if held.name == name and held.began is not None:
                shown["state"] = "running"
                shown["started"] = held.began
                shown["upkeep"] = held.upkeep is not None
                shown["made"] = grown(held.before, counted(found))
                shown["tail"] = list(held.said)
                return shown
            elif held.name == name:
                shown["state"] = "waiting"
                shown["started"] = held.started
                shown["upkeep"] = held.upkeep is not None
                shown["after"] = ahead
                return shown
            else:
                ahead.append(held.name)
        runs = runs_of(found, name)
        if len(runs) == 0:
            return shown
        else:
            started, ran = runs[-1]
            shown["started"] = started
            if ran is None:
                shown["state"] = "stopped"
                return shown
            else:
                return ended_status(shown, ran)

    def status_of_run(self, found: Places, name: Job, started: int) -> StatusData | None:
        shown = self.status(found, name)
        with self.lock:
            queue = list(self.queues.get(str(found.repo.root), []))
        for held in queue:
            if held.name == name and millis(held.started) == started:
                return shown
        for stamp, ran in runs_of(found, name):
            if millis(stamp) == started and ran is not None:
                shown["started"] = stamp
                return ended_status(shown, ran)
        return None

    def busy(self, found: Places) -> list[str]:
        names: list[str] = []
        with self.lock:
            for held in self.queues.get(str(found.repo.root), []):
                names.append(held.name)
        return names
