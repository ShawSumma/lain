from __future__ import annotations

import io
import math
import shutil
import subprocess
import tarfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from bugs import noticed
from config import Settings, reviewing
from container import Container, prepared
from extract import QUOTE, Access, Explored, Extractor, Handler, cut, diff_shown, flat, unprefixed, unsupported
from history import Call
from memory import NONE, Basis, Choice, Link, Memory, MemoryKind, Store, overlaps
from model import Failure, Usage
from page import Builder, dated, fresh_dir, items, moment, slug, when, write
from places import GIT, LAIN, Places
from prompts import message, prompt
from spans import CUT, Span, chunk, here, span
from spec import BEFORE, COMMAND, COMMIT_NOTES, CONTRADICTED, CommitArgs, ContradictedArgs, Tool
from store import JSON, locked

COMMITS = "commits"
TREES = "trees"
INDEX = "index.md"
LAYOUT = 300
README = 3000
DIFF_BUDGET = 60_000
FILE_STEP = 6000
REVIEW_BUDGET = 30_000
SMALLEST = 300
OUTPUT = 8000
RUN_SECONDS = 300.0

type Say = Callable[[str], None]


@dataclass(frozen=True)
class Changed:
    status: str
    old: str | None
    new: str | None


@dataclass(frozen=True)
class Commit:
    sha: str
    parent: str | None
    time: float
    changes: tuple[Changed, ...]

    @property
    def files(self) -> tuple[str, ...]:
        found: list[str] = []
        for change in self.changes:
            for path in (change.old, change.new):
                if path is not None and path not in found:
                    found.append(path)
        return tuple(found)


@dataclass(frozen=True)
class Move:
    old: str
    new: str
    time: float


@dataclass(frozen=True)
class Name:
    path: str
    since: float
    until: float


@dataclass(frozen=True)
class Item:
    kind: MemoryKind
    text: str
    files: tuple[str, ...]
    evidence: tuple[str, ...]
    basis: Basis
    choices: tuple[Choice, ...]


@dataclass(frozen=True)
class Read:
    message: str
    trivial: str
    items: tuple[Item, ...]
    dropped: tuple[str, ...]


def lains(path: str) -> bool:
    return PurePosixPath(path).parts[0] == LAIN


def git(root: Path, args: Sequence[str]) -> str:
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=root, capture_output=True, text=True, errors="replace").stdout


def changed_of(line: str) -> Changed | None:
    fields = line.split("\t")
    status = fields[0][:1]
    if len(fields) < 2:
        return None
    elif status in ("R", "C") and len(fields) > 2:
        return Changed(status, fields[1], fields[2])
    elif status == "A":
        return Changed(status, None, fields[1])
    elif status == "D":
        return Changed(status, fields[1], None)
    else:
        return Changed(status, fields[1], fields[1])


def outside_lain(change: Changed) -> bool:
    for path in (change.old, change.new):
        if path is not None and lains(path):
            return False
    return True


def history_of(root: Path) -> list[Commit]:
    found: list[Commit] = []
    if (root / ".git").exists():
        text = git(root, ["log", "--no-merges", "-M", "--name-status", "--format=%x01%H %ct %P", "HEAD"])
        for part in text.split("\x01")[1:]:
            head, _, body = part.partition("\n")
            words = head.split()
            parent: str | None = None
            if len(words) > 2:
                parent = words[2]
            changes: list[Changed] = []
            for line in body.splitlines():
                change = changed_of(line)
                if change is not None and outside_lain(change):
                    changes.append(change)
            found.append(Commit(words[0], parent, float(words[1]), tuple(changes)))
    return found


def analysed(home: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in items(home / INDEX, "commit"):
        sha, _, stem = line.partition(" ")
        found[sha] = stem
    return found


def layout(root: Path, sha: str) -> str:
    files: list[str] = []
    for path in git(root, ["ls-tree", "-r", "--name-only", sha]).splitlines():
        if not lains(path):
            files.append(path)
    if len(files) <= LAYOUT:
        return "\n".join(files)
    else:
        counts: dict[str, int] = {}
        for path in files:
            parts = PurePosixPath(path).parts
            key = parts[0]
            if len(parts) > 2:
                key = f"{parts[0]}/{parts[1]}/"
            elif len(parts) == 2:
                key = f"{parts[0]}/"
            counts[key] = counts.get(key, 0) + 1
        lines: list[str] = [message("layout_large", count=str(len(files)))]
        for key, count in sorted(counts.items()):
            if key.endswith("/"):
                lines.append(f"{key} ({count} files)")
            else:
                lines.append(key)
        return "\n".join(lines)


def readme(tree: Path) -> str:
    for path in sorted(tree.iterdir()):
        if path.is_file() and path.name.lower().startswith("readme"):
            return cut(path.read_text(errors="replace"), README)
    return message("none")


def parts(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") or len(found) == 0:
            found.append(line)
        else:
            found[-1] += line
    return found


def diff_of(root: Path, sha: str) -> list[str]:
    kept: list[str] = []
    for part in parts(git(root, ["show", "--format=", "-M", "--no-color", sha])):
        if part.startswith("diff --git ") and not part.removeprefix("diff --git a/").startswith(f"{LAIN}/"):
            kept.append(part)
    return kept


def shown_diff(diffs: Sequence[str], budget: int) -> str:
    limit = max(SMALLEST, min(FILE_STEP, budget // max(1, len(diffs))))
    shown: list[str] = []
    for each in diffs:
        shown.append(diff_shown(each, limit))
    return cut("\n".join(shown), budget * 2)


def change_lines(commit: Commit) -> str:
    lines: list[str] = []
    for change in commit.changes:
        match change.status:
            case "A":
                lines.append(f"added {change.new}")
            case "D":
                lines.append(f"deleted {change.old}")
            case "R" | "C":
                lines.append(f"renamed {change.old} to {change.new}")
            case _:
                lines.append(f"changed {change.new}")
    return "\n".join(lines)


def exported(root: Path, sha: str, target: Path) -> None:
    data = subprocess.run(["git", "archive", "--format=tar", sha], cwd=root, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(target, filter="data")


class Looking:
    root: Path
    tree: Path
    parent: str | None
    container: Container | None

    def __init__(self, root: Path, tree: Path, parent: str | None, container: Container | None) -> None:
        self.root = root
        self.tree = tree
        self.parent = parent
        self.container = container

    def before(self, call: Call) -> str:
        path = str(call.given().get("path", ""))
        if self.parent is None:
            return message("before_none", path=path)
        else:
            done = subprocess.run(["git", "show", f"{self.parent}:{path}"], cwd=self.root, capture_output=True, text=True, errors="replace")
            if done.returncode != 0:
                return message("before_missing", path=path)
            else:
                return done.stdout

    def command(self, call: Call) -> str:
        argv = ["sh", "-c", str(call.given().get("command", ""))]
        if self.container is not None:
            argv = self.container.argv(self.tree, argv)
        try:
            done = subprocess.run(argv, cwd=self.tree, capture_output=True, text=True, errors="replace", timeout=RUN_SECONDS)
        except subprocess.TimeoutExpired:
            return message("command_slow", seconds=f"{RUN_SECONDS:.0f}")
        else:
            return message("command_ran", code=str(done.returncode), output=cut(f"{done.stdout}{done.stderr}".strip(), OUTPUT))

    def access(self) -> Access:
        extra: list[tuple[Tool, Handler]] = [(BEFORE, self.before)]
        if self.container is not None:
            extra.append((COMMAND, self.command))
        return Access(self.tree, self.tree, (self.tree,), tuple(extra), ())


def seen_texts(explored: Explored) -> list[str]:
    found: list[str] = []
    for each in explored.messages:
        if each["role"] == "tool":
            found.append(flat(each["content"]))
    return found


def shown_in(quote: str, sources: Sequence[str]) -> bool:
    wanted = flat(quote)
    if len(wanted) < QUOTE:
        return False
    else:
        for source in sources:
            if wanted in source:
                return True
        return False


def graded(evidence: Sequence[str], sources: Sequence[str], deciding: bool) -> Basis:
    if len(evidence) == 0:
        return "inferred"
    else:
        for quote in evidence:
            if not shown_in(quote, sources):
                return "inferred"
        if deciding:
            return "inferred"
        else:
            return "verified"


def kept_files(tree: Path, commit: Commit, files: Sequence[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for raw in files:
        path = raw.strip().removeprefix("./")
        if path != "" and (path in commit.files or (tree / path).exists()) and path not in kept:
            kept.append(path)
    return tuple(kept)


def sources_of(tree: Path, diffs: Sequence[str], explored: Explored, files: Sequence[str]) -> list[str]:
    found = [flat(unprefixed("".join(diffs))), *seen_texts(explored)]
    for path in files:
        if (tree / path).is_file():
            found.append(flat((tree / path).read_text(errors="replace")))
    return found


def items_of(tree: Path, commit: Commit, diffs: Sequence[str], explored: Explored) -> tuple[list[Item], list[str]]:
    answer = cast(CommitArgs, explored.answer)
    made: list[Item] = []
    dropped: list[str] = []
    for decision in answer["decisions"]:
        files = kept_files(tree, commit, decision["files"])
        choices: list[Choice] = []
        whole = [decision["chose"]]
        for option in decision["options"]:
            choices.append(Choice(f"{option['option']}: {option['verdict']}", option["taken"]))
            whole.append(f"{option['option']} {option['verdict']}")
        missing = unsupported(" ".join(whole), decision["evidence"], files)
        if len(missing) > 0:
            dropped.append(f"decision dropped, because {', '.join(missing)} is not in its evidence: {decision['chose']}")
        else:
            basis = graded(decision["evidence"], sources_of(tree, diffs, explored, files), True)
            made.append(Item("decision", decision["chose"].strip(), files, tuple(decision["evidence"]), basis, tuple(choices)))
    for fact in answer["facts"]:
        files = kept_files(tree, commit, fact["files"])
        missing = unsupported(fact["text"], fact["evidence"], files)
        if len(missing) > 0:
            dropped.append(f"{fact['kind']} dropped, because {', '.join(missing)} is not in its evidence: {fact['text']}")
        else:
            basis = graded(fact["evidence"], sources_of(tree, diffs, explored, files), False)
            made.append(Item(fact["kind"], fact["text"].strip(), files, tuple(fact["evidence"]), basis, ()))
    return made, dropped


def empty(answer: CommitArgs) -> bool:
    return len(answer["decisions"]) + len(answer["facts"]) == 0 and answer["trivial"].strip() == ""


class Scan:
    found: Places
    store: Store
    settings: Settings
    extractor: Extractor
    container: Container | None
    say: Say
    parent: str | None
    guard: threading.Lock
    failed: list[str]
    streak: int
    halted: bool

    def __init__(self, found: Places, store: Store, settings: Settings, usage: Usage, container: Container | None, say: Say) -> None:
        self.found = found
        self.store = store
        self.settings = settings
        self.extractor = Extractor(found, settings, usage)
        self.container = container
        self.say = say
        self.parent = here()
        self.guard = threading.Lock()
        self.failed = []
        self.streak = 0
        self.halted = False

    def one(self, commit: Commit) -> None:
        given: dict[str, JSON] = {"sha": commit.sha, "files": len(commit.changes)}
        with span("commit", f"{moment(commit.time)} {commit.sha[:12]}", str(self.found.repo.root), self.parent, given) as doing:
            tree = fresh_dir(self.found.agent(GIT) / TREES, commit.time)
            try:
                done = self.read(commit, tree)
            finally:
                shutil.rmtree(tree, ignore_errors=True)
            with self.guard:
                if isinstance(done, Failure):
                    doing.ok = False
                    doing.attrs["reason"] = cut(done.reason, CUT)
                    self.failing(f"{commit.sha[:12]}: {done.reason}")
                else:
                    self.streak = 0
                    self.kept(commit, done, doing)

    def read(self, commit: Commit, tree: Path) -> Read | Failure:
        root = self.found.repo.root
        exported(root, commit.sha, tree)
        diffs = diff_of(root, commit.sha)
        looking = Looking(root, tree, commit.parent, self.container)
        access = looking.access()
        text = message(
            "commit_record",
            sha=commit.sha,
            date=moment(commit.time),
            count=str(len(commit.changes)),
            layout=layout(root, commit.sha),
            readme=readme(tree),
            changes=change_lines(commit),
            diff=shown_diff(diffs, DIFF_BUDGET),
        )
        system = prompt("commit")
        explored = self.extractor.explore(system, text, COMMIT_NOTES, access, commit.sha[:12], self.settings)
        if not isinstance(explored, Failure) and empty(cast(CommitArgs, explored.answer)):
            explored = self.extractor.again(explored, message("commit_empty"), COMMIT_NOTES, access, self.settings)
        if isinstance(explored, Failure):
            return explored
        else:
            answer = cast(CommitArgs, explored.answer)
            made, dropped = items_of(tree, commit, diffs, explored)
            return self.reviewed(commit, diffs, Read(answer["message"].strip(), answer["trivial"].strip(), tuple(made), tuple(dropped)))

    def reviewed(self, commit: Commit, diffs: Sequence[str], done: Read) -> Read | Failure:
        if len(done.items) == 0:
            return done
        else:
            listed: list[str] = []
            for number, item in enumerate(done.items, 1):
                listed.append(f"{number}. {item.kind}: {item.text} (evidence: {' | '.join(item.evidence)})")
            text = message("contradict_items", record=shown_diff(diffs, REVIEW_BUDGET), items="\n".join(listed))
            answer = self.extractor.answered(prompt("contradict", root="."), text, CONTRADICTED, commit.sha[:12], reviewing(self.settings))
            if isinstance(answer, Failure):
                return answer
            else:
                gone: dict[int, str] = {}
                for each in cast(ContradictedArgs, answer.answer)["contradicted"]:
                    gone[each["item"]] = each["why"].strip()
                kept: list[Item] = []
                dropped = list(done.dropped)
                for number, item in enumerate(done.items, 1):
                    if number in gone:
                        dropped.append(f"{item.kind} dropped by the review, because {gone[number]}: {item.text}")
                    else:
                        kept.append(item)
                return Read(done.message, done.trivial, tuple(kept), tuple(dropped))

    def failing(self, reason: str) -> None:
        self.failed.append(reason)
        self.say(f"not read, and tried again next time: {reason}")
        self.streak += 1
        if self.streak >= self.settings.failure_streak and not self.halted:
            summary = f"reading git history stopped after {self.streak} commits in a row failed; the last said: {reason}"
            noticed("commits", str(self.found.repo.root), summary, "\n".join(self.failed[-self.streak :]))
            self.say(summary)
            self.halted = True

    def kept(self, commit: Commit, done: Read, doing: Span) -> None:
        home = self.found.agent(GIT)
        subject = done.message.partition("\n")[0]
        base = dated(commit.time)
        if slug(subject) != "":
            base = f"{base}-{slug(subject)}"
        stem = base
        count = 1
        while (home / COMMITS / f"{stem}.md").exists():
            count += 1
            stem = f"{base}-{count}"
        agent = f"{GIT}/{stem}"
        goal = self.store.add("goal", subject, "inferred", f"{agent}/0", commit.time, commit.files, (), (), (), NONE)
        made: list[Memory] = []
        for number, item in enumerate(done.items, 1):
            source = f"{agent}/{number}"
            if item.kind == "decision":
                made.append(self.store.decide(goal, item.text, item.choices, item.files, item.basis, source, commit.time, (), item.evidence, NONE))
            else:
                made.append(self.store.add(item.kind, item.text, item.basis, source, commit.time, item.files, (Link("leads_to", goal),), (), item.evidence, NONE))
        built = Builder(f"commit {commit.sha[:12]} of {moment(commit.time)}: {subject}")
        built.field("sha", commit.sha)
        built.field("parent", commit.parent or "none")
        built.field("time", when(commit.time))
        for change in commit.changes:
            built.field("change", change_lines(Commit(commit.sha, None, commit.time, (change,))))
        built.field("goal", goal.id)
        for memory in made:
            built.field("memory", memory.id)
        if done.trivial != "":
            built.field("trivial", " ".join(done.trivial.split()))
        built.section("message", done.message)
        for reason in done.dropped:
            built.section("dropped", reason)
        write(home / COMMITS / f"{stem}.md", built.page())
        with locked(home):
            index = analysed(home)
            index[commit.sha] = stem
            lines: list[str] = []
            for sha, name in index.items():
                lines.append(f"{sha} {name}")
            write_index(home, lines)
        for memory in made:
            chunk(doing, "note", f"{memory.kind} ({memory.basis}) {memory.id}: {memory.text}")
        for reason in done.dropped:
            chunk(doing, "note", reason)
        doing.attrs["remembered"] = len(made)
        self.say(f"{moment(commit.time)} {subject}: {len(made)} remembered")


def write_index(home: Path, lines: Sequence[str]) -> None:
    built = Builder("the commits lain has read, and the page for each")
    for line in lines:
        built.field("commit", line)
    write(home / INDEX, built.page())


def scan(found: Places, settings: Settings, usage: Usage, say: Say, stop: threading.Event) -> int:
    home = found.agent(GIT)
    every = history_of(found.repo.root)
    done = analysed(home)
    todo: list[Commit] = []
    for commit in every:
        if commit.sha not in done:
            todo.append(commit)
    say(f"git: {len(every)} commits, {len(every) - len(todo)} read before, {len(todo)} to read, newest first")
    container: Container | None = None
    if settings.commands == "docker":
        made = prepared(found.repo, settings)
        if isinstance(made, Failure):
            say(f"commands are not offered: {made.reason}")
        else:
            container = made
    run = Scan(found, Store(found.memory), settings, usage, container, say)
    with ThreadPoolExecutor(max_workers=settings.parallel) as pool:
        for first in range(0, len(todo), settings.parallel):
            if not stop.is_set() and not run.halted:
                list(pool.map(run.one, todo[first : first + settings.parallel]))
    return len(run.failed)


def reached(files: Sequence[str], stamp: float, names: Sequence[Name]) -> bool:
    for name in names:
        if overlaps(files, (name.path,)) and name.since <= stamp <= name.until:
            return True
    return False


class Past:
    commits: list[Commit]
    moves: list[Move]

    def __init__(self, root: Path) -> None:
        self.commits = list(reversed(history_of(root)))
        self.moves = []
        for commit in self.commits:
            for change in commit.changes:
                if change.status == "R" and change.old is not None and change.new is not None:
                    self.moves.append(Move(change.old, change.new, commit.time))

    def names(self, path: str) -> list[Name]:
        found: list[Name] = []
        current = path
        until = math.inf
        while True:
            into: Move | None = None
            for move in self.moves:
                if move.new == current and move.time < until and (into is None or move.time > into.time):
                    into = move
            if into is None:
                found.append(Name(current, -math.inf, until))
                return found
            else:
                found.append(Name(current, into.time, until))
                current = into.old
                until = into.time

    def current(self, path: str, stamp: float) -> str:
        now = path
        since = stamp
        first = True
        while True:
            out: Move | None = None
            for move in self.moves:
                later = move.time > since or (first and move.time == since)
                if move.old == now and later and (out is None or move.time < out.time):
                    out = move
            if out is None:
                return now
            else:
                now = out.new
                since = out.time
                first = False

    def changes(self, path: str) -> list[Commit]:
        names = self.names(path)
        found: list[Commit] = []
        for commit in self.commits:
            if reached(commit.files, commit.time, names):
                found.append(commit)
        return found

    def together(self, path: str, root: Path) -> list[tuple[str, int]]:
        names = self.names(path)
        own: set[str] = set()
        for name in names:
            own.add(name.path)
        counts: dict[str, int] = {}
        for commit in self.changes(path):
            seen: set[str] = set()
            for other in commit.files:
                now = self.current(other, commit.time)
                if other not in own and now != path and now not in seen and (root / now).is_file():
                    seen.add(now)
                    counts[now] = counts.get(now, 0) + 1
        return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
