from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from agent import paths_changed
from bugs import noticed
from config import Settings, reviewing
from context import assistant
from finder import Finder
from history import Brief, Call, History, Note, Reply, Request, Result, Step
from ingest import live, read_state, write_state
from memory import NONE, TRUST, Basis, Choice, Conditions, Link, Memory, MemoryKind, Store, lead, overlaps, present, scoped
from model import Client, Failure, Message, Usage
from places import LAIN, REVIEWS, Places
from prompts import message, prompt
from rank import scores, words
from spans import CUT, Span, chunk, here, span
from spec import CONTRADICTED, EXTRACT, LIST_DIR, READ_FILE, SEARCH, ContradictedArgs, DecisionData, ExtractArgs, Tool, misfit
from store import JSON, locked
from tools import Toolbox

NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)*")
DIRECTORY = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+)(?!\w)")
GOALS = 8
QUOTE = 8
EVIDENCE = 2000
NEW_FILE = 1500
SMALLEST = 300
EARLIER = 15
GIST = 240
TRIES = 2


@dataclass(frozen=True)
class Stretch:
    agent: str
    steps: tuple[Step, ...]

    @property
    def request(self) -> Step:
        return self.steps[0]

    @property
    def asked(self) -> str:
        match self.request.body:
            case Request(text) | Brief(text=text):
                return text
            case _:
                return ""

    def numbers(self) -> set[int]:
        found: set[int] = set()
        for step in self.steps:
            found.add(step.number)
        return found

    def step(self, number: int) -> Step | None:
        for step in self.steps:
            if step.number == number:
                return step
        return None

    def ref(self, number: int) -> str:
        return f"{self.agent}/{number}"


@dataclass(frozen=True)
class Pivot:
    path: str
    edit: int
    failed: int
    again: int
    passed: int
    command: str


@dataclass(frozen=True)
class Extracted:
    made: list[Memory]
    dropped: list[str]
    failed: list[str]


@dataclass(frozen=True)
class Remembered:
    made: list[Memory]
    dropped: list[str]


@dataclass(frozen=True)
class Extraction:
    found: ExtractArgs
    dropped: tuple[str, ...]


@dataclass(frozen=True)
class Weighed:
    item: DecisionData
    basis: Basis
    cites: tuple[int, ...]
    files: tuple[str, ...]
    cited: tuple[str, ...]
    missing: list[str]


@dataclass(frozen=True)
class Graded:
    kind: MemoryKind
    text: str
    files: tuple[str, ...]
    cites: tuple[int, ...]
    basis: Basis
    cited: tuple[str, ...]
    missing: list[str]
    restates: tuple[str, ...]


def stretches(agent: str, history: History, done: set[int], final: bool) -> list[Stretch]:
    found: list[list[Step]] = []
    for step in history.lineage():
        match step.body:
            case Request() | Brief():
                found.append([step])
            case _ if len(found) > 0:
                found[-1].append(step)
            case _:
                pass
    kept: list[Stretch] = []
    for index, steps in enumerate(found):
        complete = final or index < len(found) - 1
        if complete and steps[0].number not in done and len(steps) > 1:
            kept.append(Stretch(agent, tuple(steps)))
    return kept


def answering(stretch: Stretch) -> dict[int, Call]:
    latest: dict[str, Call] = {}
    found: dict[int, Call] = {}
    for step in stretch.steps:
        match step.body:
            case Reply(calls=calls):
                for call in calls:
                    latest[call.id] = call
            case Result(call=ident) if ident in latest:
                found[step.number] = latest[ident]
            case _:
                pass
    return found


def argument(call: Call | None) -> str:
    if call is None:
        return ""
    else:
        data = call.given()
        for key in ("file_path", "command", "pattern", "path", "url", "description"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        return ""


def result_line(number: int, result: Result, call: Call | None) -> str:
    verdict = "failed"
    if result.ok:
        verdict = "ok"
    shown = result.text
    if result.tool == "Read" and result.ok:
        shown = f"{len(result.text.splitlines())} lines"
    paths: list[str] = []
    for change in result.patch:
        paths.append(change.path)
    extra = ""
    if len(paths) > 0:
        extra = f" (changed {', '.join(paths)})"
    return f"[{number}] {result.tool} {argument(call)} -> {verdict}{extra}: {shown}"


def step_lines(step: Step, calls: dict[int, Call]) -> list[str]:
    lines: list[str] = []
    match step.body:
        case Request(text):
            lines.append(f"[{step.number}] user: {text}")
        case Brief(text=text, origin=origin):
            lines.append(f"[{step.number}] brief from {origin}: {text}")
        case Note(text):
            lines.append(f"[{step.number}] note: {text}")
        case Reply(content=content, calls=made):
            if content.strip() != "":
                lines.append(f"[{step.number}] agent says: {content}")
            for call in made:
                lines.append(f"[{step.number}] agent calls {call.tool} {call.arguments}")
        case Result() as result:
            lines.append(result_line(step.number, result, calls.get(step.number)))
        case _:
            pass
    return lines


def cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    else:
        half = limit // 2
        return f"{text[:half]}\n{message('cut', count=str(len(text) - 2 * half))}\n{text[-half:]}"


def condensed(stretch: Stretch, limit: int) -> str:
    calls = answering(stretch)
    lines: list[str] = []
    for step in stretch.steps:
        match step.body:
            case Result(tool="read_file", ok=True) as result:
                read = replace(result, text=message("read_lines", count=str(result.text.count("\n"))))
                lines.append(result_line(step.number, read, calls.get(step.number)))
            case _:
                for line in step_lines(step, calls):
                    lines.append(cut(line, limit))
    return "\n".join(lines)


def whole_file(text: str, limit: int) -> str:
    lines = text.splitlines()
    body = 0
    for index, line in enumerate(lines):
        if line.startswith("@@"):
            body = index + 1
            break
    kept: list[str] = []
    used = 0
    for line in lines[body:]:
        used += len(line) + 1
        if used > limit:
            break
        else:
            kept.append(line)
    left = len(lines) - body - len(kept)
    if left > 0:
        kept.append(message("diff_more", count=str(left)))
    return "\n".join([*lines[:body], *kept])


def diff_shown(text: str, limit: int) -> str:
    head = text[:400]
    if "\ndeleted file mode" in head:
        return f"{text.partition(chr(10))[0]}\n{message('diff_deleted', count=str(text.count(chr(10) + '-') - 1))}"
    elif "\nnew file mode" in head:
        return whole_file(text, min(limit, NEW_FILE))
    else:
        return cut(text, limit)


def step_shown(step: Step, calls: dict[int, Call], limit: int) -> list[str]:
    match step.body:
        case Result(tool="diff", text=text) as result:
            return [result_line(step.number, replace(result, text=diff_shown(text, limit)), calls.get(step.number))]
        case _:
            lines: list[str] = []
            for line in step_lines(step, calls):
                lines.append(cut(line, limit))
            return lines


def rendered(stretch: Stretch, only: set[int] | None, limit: int) -> str:
    calls = answering(stretch)
    lines: list[str] = []
    for step in stretch.steps:
        if only is None or step.number in only:
            lines.extend(step_shown(step, calls, limit))
    return "\n".join(lines)


def fitted(stretch: Stretch, settings: Settings) -> str:
    per_step = min(settings.record_step, settings.record_budget // max(1, len(stretch.steps)))
    return rendered(stretch, None, max(SMALLEST, per_step))


def changed_files(stretch: Stretch) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []
    for step in stretch.steps:
        if isinstance(step.body, Result):
            for path in paths_changed(step):
                found.append((path, step.number))
    return found


def changed_paths(stretch: Stretch) -> list[str]:
    found: list[str] = []
    for path, _ in changed_files(stretch):
        if path not in found:
            found.append(path)
    return sorted(found)


def commands(stretch: Stretch) -> list[tuple[str, int, bool]]:
    calls = answering(stretch)
    found: list[tuple[str, int, bool]] = []
    for step in stretch.steps:
        match step.body:
            case Result(tool="Bash" | "run", ok=ok):
                found.append((argument(calls.get(step.number)), step.number, ok))
            case _:
                pass
    return found


def later_pass(runs: Sequence[tuple[str, int, bool]], command: str, after: int) -> int | None:
    for name, number, ok in runs:
        if name == command and ok and number > after:
            return number
    return None


def numbers_of(edits: Sequence[tuple[str, int]], path: str, low: int, high: int) -> list[int]:
    found: list[int] = []
    for other, number in edits:
        if other == path and low < number < high:
            found.append(number)
    return found


def fixed(found: Sequence[Pivot], path: str, passed: int) -> bool:
    for pivot in found:
        if pivot.path == path and pivot.passed == passed:
            return True
    return False


def pivots(stretch: Stretch) -> list[Pivot]:
    edits = changed_files(stretch)
    runs = commands(stretch)
    found: list[Pivot] = []
    for command, failed, ok in runs:
        passed = later_pass(runs, command, failed)
        if not ok and passed is not None:
            for path in changed_paths(stretch):
                before = numbers_of(edits, path, 0, failed)
                between = numbers_of(edits, path, failed, passed)
                if len(before) > 0 and len(between) > 0 and not fixed(found, path, passed):
                    found.append(Pivot(path, before[-1], failed, between[0], passed, command))
    return found


def checks(stretch: Stretch) -> list[int]:
    last = 0
    for _, number in changed_files(stretch):
        last = max(last, number)
    found: list[int] = []
    for _, number, ok in commands(stretch):
        if ok and number > last:
            found.append(number)
    return found


def numbers_in(text: str) -> set[str]:
    found: set[str] = set()
    for number in NUMBER.findall(text):
        found.add(number.replace(",", ""))
    return found


def unsupported(text: str, cited: Sequence[str], files: Sequence[str]) -> list[str]:
    shown = numbers_in(f"{' '.join(cited)} {' '.join(files)}")
    return sorted(numbers_in(text) - shown)


def dropped_text(kind: str, text: str, missing: Sequence[str]) -> str:
    return f"{kind} not kept, because {', '.join(missing)} is not in the steps it cites: {text}"


def pivot_text(pivot: Pivot) -> str:
    return (
        f"{pivot.command} failed at step {pivot.failed} after {pivot.path} was changed at step {pivot.edit}; "
        f"{pivot.path} was changed again at step {pivot.again}, and the command passed at step {pivot.passed}"
    )


def facts(stretch: Stretch) -> str:
    lines: list[str] = ["Files changed:"]
    for path, number in changed_files(stretch):
        lines.append(f"- {path} at step {number}")
    lines.append("Commands run:")
    for command, number, ok in commands(stretch):
        if ok:
            lines.append(f"- step {number}: {command} passed")
        else:
            lines.append(f"- step {number}: {command} failed")
    lines.append("Approaches dropped after a failure:")
    for pivot in pivots(stretch):
        lines.append(f"- {pivot_text(pivot)}")
    return "\n".join(lines)


def before(memory: Memory, agent: str, number: int) -> bool:
    return memory.agent != agent or memory.step < number


def earlier(store: Store, stretch: Stretch) -> str:
    files = changed_paths(stretch)
    query = f"{stretch.asked} {' '.join(files)}"
    decisions: list[Memory] = []
    for memory in store.open_decisions(files):
        if before(memory, stretch.agent, stretch.request.number):
            decisions.append(memory)
    rules: list[Memory] = []
    for memory in store.ordered():
        if memory.kind == "observation" and memory.basis == "user" and before(memory, stretch.agent, stretch.request.number):
            rules.append(memory)
    lines: list[str] = ["Earlier decisions still open on the files changed, by id:"]
    for memory in store.ranked(decisions, query)[:EARLIER]:
        lines.append(f"- {memory.id}: {cut(flat(memory.text), GIST)} (files: {', '.join(store.current_files(memory))})")
    lines.append("Rules the user gave earlier, by id:")
    for memory in store.ranked(rules, query)[:EARLIER]:
        lines.append(f"- {memory.id}: {cut(flat(memory.text), GIST)}")
    return "\n".join(lines)


def earlier_goals(store: Store, stretch: Stretch) -> str:
    found: list[Memory] = []
    for memory in store.ordered():
        if memory.kind == "goal" and memory.basis == "user" and memory.text.strip() != "" and before(memory, stretch.agent, stretch.request.number):
            found.append(memory)
    documents: list[set[str]] = []
    for memory in found:
        documents.append(words(memory.text))
    ranked = sorted(zip(scores(stretch.asked, documents), range(len(found))), key=lambda pair: -pair[0])
    lines: list[str] = ["What earlier work set out to do, closest first, by id:"]
    for score, index in ranked[:GOALS]:
        if score > 0:
            lines.append(f"- {found[index].id}: {found[index].text.strip().partition(chr(10))[0]}")
    return "\n".join(lines)


def record_of(stretch: Stretch, store: Store, settings: Settings) -> str:
    parts = [fitted(stretch, settings), facts(stretch), earlier(store, stretch)]
    if stretch.agent != REVIEWS:
        parts.append(earlier_goals(store, stretch))
    return cut("\n\n".join(parts), settings.prompt_limit * 3 // 4)


def prompt_name(agent: str) -> str:
    if agent == REVIEWS:
        return "extract_pr"
    else:
        return "extract"


@dataclass(frozen=True)
class Explored:
    answer: dict[str, JSON]
    system: str
    session: str
    messages: tuple[Message, ...]
    call: str
    subject: str


type Handler = Callable[[Call], str]


@dataclass(frozen=True)
class Access:
    cwd: Path
    root: Path
    directories: tuple[Path, ...]
    extra: tuple[tuple[Tool, Handler], ...]
    hidden: tuple[Path, ...]


def answered_calls(sent: list[Message], calls: Sequence[Call], tools: Sequence[Tool], tool: Tool, handlers: dict[str, Handler], toolbox: Toolbox) -> Call | None:
    finishing: Call | None = None
    for call in calls:
        problem = misfit(tools, call.tool, call.arguments)
        if problem is not None:
            sent.append({"role": "tool", "tool_call_id": call.id, "content": message("misfit", tool=call.tool, problem=problem)})
        elif call.tool == tool.name and finishing is None:
            finishing = call
        elif call.tool == tool.name:
            sent.append({"role": "tool", "tool_call_id": call.id, "content": message("finish_once")})
        elif call.tool in handlers:
            sent.append({"role": "tool", "tool_call_id": call.id, "content": handlers[call.tool](call)})
        else:
            sent.append({"role": "tool", "tool_call_id": call.id, "content": toolbox.dispatch(call).text})
    return finishing


class Extractor:
    found: Places
    settings: Settings
    usage: Usage

    def __init__(self, found: Places, settings: Settings, usage: Usage) -> None:
        self.found = found
        self.settings = settings
        self.usage = usage

    def access(self) -> Access:
        root = self.found.repo.root
        return Access(root, root, (root,), (), (root / LAIN,))

    def answered(self, system: str, text: str, tool: Tool, subject: str, settings: Settings) -> Explored | Failure:
        sent: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        return self.again_once(system, sent, tool, subject, settings, TRIES)

    def again_once(self, system: str, sent: list[Message], tool: Tool, subject: str, settings: Settings, tries: int) -> Explored | Failure:
        client = Client(self.usage, settings, subject, self.found.repo.root)
        for _ in range(tries):
            answer = client.answer(sent, tool, settings.effort)
            if isinstance(answer, Failure):
                return answer
            else:
                sent.append(assistant(Reply(answer.reasoning, answer.content, answer.calls, answer.finish, answer.tokens, answer.seconds)))
                finishing: Call | None = None
                for call in answer.calls:
                    problem = misfit((tool,), call.tool, call.arguments)
                    if problem is None and finishing is None:
                        finishing = call
                    elif problem is None:
                        sent.append({"role": "tool", "tool_call_id": call.id, "content": message("finish_once")})
                    else:
                        sent.append({"role": "tool", "tool_call_id": call.id, "content": message("misfit", tool=call.tool, problem=problem)})
                if finishing is not None:
                    return Explored(json.loads(finishing.arguments), system, "", tuple(sent), finishing.id, subject)
                elif len(answer.calls) == 0:
                    sent.append({"role": "user", "content": message("answer_with", tool=tool.name)})
        return Failure(f"the model gave no usable {tool.name} call in {tries} tries")

    def answered_again(self, earlier: Explored, text: str, tool: Tool, settings: Settings) -> Explored | Failure:
        sent: list[Message] = [*earlier.messages, {"role": "tool", "tool_call_id": earlier.call, "content": text}]
        return self.again_once(earlier.system, sent, tool, earlier.subject, settings, TRIES)

    def explore(self, system: str, text: str, tool: Tool, access: Access, subject: str, settings: Settings) -> Explored | Failure:
        sent: list[Message] = [{"role": "system", "content": system}, {"role": "user", "content": text}]
        return self.converse(system, sent, tool, access, subject, settings)

    def again(self, earlier: Explored, text: str, tool: Tool, access: Access, settings: Settings) -> Explored | Failure:
        sent: list[Message] = [*earlier.messages, {"role": "tool", "tool_call_id": earlier.call, "content": text}]
        return self.converse(earlier.system, sent, tool, access, earlier.subject, settings)

    def converse(self, system: str, sent: list[Message], tool: Tool, access: Access, subject: str, settings: Settings) -> Explored | Failure:
        client = Client(self.usage, settings, subject, access.root)
        toolbox = Toolbox(access.root, {}, (), Finder(self.usage, settings), access.root, None)
        handlers: dict[str, Handler] = {}
        tools: list[Tool] = [LIST_DIR, READ_FILE, SEARCH]
        for extra, handler in access.extra:
            tools.append(extra)
            handlers[extra.name] = handler
        tools.append(tool)
        idle = False
        for _ in range(settings.explore_turns):
            answer = client.chat(sent, tuple(tools), settings.effort)
            if isinstance(answer, Failure):
                return answer
            else:
                sent.append(assistant(Reply(answer.reasoning, answer.content, answer.calls, answer.finish, answer.tokens, answer.seconds)))
                finishing = answered_calls(sent, answer.calls, tools, tool, handlers, toolbox)
                if finishing is not None:
                    return Explored(json.loads(finishing.arguments), system, "", tuple(sent), finishing.id, subject)
                elif len(answer.calls) == 0 and idle:
                    return Failure(f"the model stopped twice without calling {tool.name}")
                elif len(answer.calls) == 0:
                    idle = True
                    sent.append({"role": "user", "content": message("answer_with", tool=tool.name)})
                else:
                    idle = False
        return Failure(message("explore_turns", tool=tool.name, turns=str(settings.explore_turns)))

    def extract(self, stretch: Stretch, record: str) -> Extraction | Failure:
        system = prompt(prompt_name(stretch.agent), root=".")
        found = self.answered(system, record, EXTRACT, stretch.ref(stretch.request.number), self.settings)
        if isinstance(found, Failure):
            return found
        else:
            return self.screen(stretch, cast(ExtractArgs, found.answer))

    def screen(self, stretch: Stretch, found: ExtractArgs) -> Extraction | Failure:
        items: list[tuple[str, int, str]] = []
        cited: set[int] = {stretch.request.number}
        for index, decision in enumerate(found["decisions"]):
            items.append(("decisions", index, decision["chose"]))
            cited.update(decision["cites"])
        for index, claim in enumerate(found["claims"]):
            items.append(("claims", index, claim["text"]))
            cited.update(claim["cites"])
        if len(items) == 0:
            return Extraction(found, ())
        else:
            listed: list[str] = []
            for number, (_, _, text) in enumerate(items, 1):
                listed.append(f"{number}. {text}")
            text = message("contradict_items", record=rendered(stretch, cited, self.settings.record_step), items="\n".join(listed))
            system = prompt("contradict", root=".")
            answer = self.answered(system, text, CONTRADICTED, stretch.ref(stretch.request.number), reviewing(self.settings))
            if isinstance(answer, Failure):
                return answer
            else:
                gone: dict[tuple[str, int], str] = {}
                for each in cast(ContradictedArgs, answer.answer)["contradicted"]:
                    if 1 <= each["item"] <= len(items):
                        section, index, _ = items[each["item"] - 1]
                        gone[(section, index)] = each["why"].strip()
                kept: ExtractArgs = {"decisions": [], "claims": [], "continues": found["continues"]}
                dropped: list[str] = []
                for index, decision in enumerate(found["decisions"]):
                    if ("decisions", index) in gone:
                        dropped.append(message("contradicted", kind="decision", text=decision["chose"], why=gone[("decisions", index)]))
                    else:
                        kept["decisions"].append(decision)
                for index, claim in enumerate(found["claims"]):
                    if ("claims", index) in gone:
                        dropped.append(message("contradicted", kind=claim["kind"], text=claim["text"], why=gone[("claims", index)]))
                    else:
                        kept["claims"].append(claim)
                return Extraction(kept, tuple(dropped))


def flat(text: str) -> str:
    return " ".join(text.split())


def strings_of(value: JSON) -> list[str]:
    if isinstance(value, str):
        return [value]
    elif isinstance(value, list):
        found: list[str] = []
        for item in value:
            found.extend(strings_of(item))
        return found
    elif isinstance(value, dict):
        found = []
        for item in value.values():
            found.extend(strings_of(item))
        return found
    else:
        return []


def unprefixed(diff: str) -> str:
    lines: list[str] = []
    for line in diff.splitlines():
        if line[:1] in ("+", "-", " ") and not line.startswith(("+++", "---")):
            lines.append(line[1:])
        else:
            lines.append(line)
    return "\n".join(lines)


def said_in(step: Step, call: Call | None) -> tuple[str, Basis | None, bool]:
    match step.body:
        case Request(text):
            return flat(text), "user", True
        case Reply(content=content):
            return flat(content), "stated", True
        case Result(tool="diff" | "rename", text=text, ok=ok):
            return flat(unprefixed(text)), "verified", ok
        case Result(tool="comment", text=text):
            return flat(text), "stated", True
        case Result(text=text, ok=ok) as result:
            parts = [result_line(step.number, result, call), text]
            if call is not None:
                parts.extend(strings_of(call.given()))
            return flat("\n".join(parts)), "verified", ok
        case Brief(text=text) | Note(text):
            return flat(text), "inferred", True
        case _:
            return "", None, True


def quoted_basis(stretch: Stretch, cites: Sequence[int], quote: str, succeeded: bool) -> Basis | None:
    calls = answering(stretch)
    wanted = flat(quote)
    best: Basis | None = None
    if len(wanted) >= QUOTE:
        for number in cites:
            step = stretch.step(number)
            if step is not None:
                text, basis, ok = said_in(step, calls.get(number))
                if basis is not None and wanted in text and (ok or not succeeded or basis != "verified"):
                    if best is None or TRUST.index(basis) < TRUST.index(best):
                        best = basis
    return best


def grade(stretch: Stretch, cites: Sequence[int], quotes: Sequence[str], succeeded: bool) -> tuple[Basis, tuple[int, ...]]:
    valid: list[int] = []
    for number in cites:
        if stretch.step(number) is not None and number not in valid:
            valid.append(number)
    weakest: Basis = "inferred"
    if len(quotes) > 0:
        weakest = "user"
        for quote in quotes:
            basis = quoted_basis(stretch, valid, quote, succeeded) or "inferred"
            if TRUST.index(basis) > TRUST.index(weakest):
                weakest = basis
    return weakest, tuple(valid)


def misquoted(stretch: Stretch, cites: Sequence[int], quotes: Sequence[str]) -> list[str]:
    found: list[str] = []
    for quote in quotes:
        if quoted_basis(stretch, cites, quote, False) is None:
            found.append(quote)
    return found


def decided(basis: Basis) -> Basis:
    if basis == "verified":
        return "inferred"
    else:
        return basis


def known_files(stretch: Stretch, root: Path, files: Sequence[str]) -> tuple[str, ...]:
    touched = set(changed_paths(stretch))
    for step in stretch.steps:
        match step.body:
            case Result(reads=reads) if reads is not None:
                for seen in reads:
                    if seen.kind != "bound":
                        touched.add(seen.path)
            case _:
                pass
    kept: list[str] = []
    for path in files:
        clean = path.strip()
        if clean == "./":
            clean = "."
        elif clean != ".":
            clean = clean.removeprefix("./")
        if clean != "" and not scoped(clean) and (root / clean).is_dir():
            clean = f"{clean}/"
        if clean != "" and (clean in touched or present(root, clean)) and clean not in kept:
            kept.append(clean)
    return tuple(kept)


def evidence(stretch: Stretch, cites: Sequence[int]) -> tuple[str, ...]:
    found: list[str] = []
    for number in cites:
        found.append(rendered(stretch, {number}, EVIDENCE))
    return tuple(found)


def cited_step(stretch: Stretch, cites: Sequence[int]) -> Step:
    for number in cites:
        step = stretch.step(number)
        if step is not None:
            return step
    return stretch.request


def widened(root: Path, text: str, files: tuple[str, ...]) -> tuple[str, ...]:
    found = list(files)
    for path in DIRECTORY.findall(text):
        if (root / path).is_dir() and path not in found:
            found.append(path)
    return tuple(found)


def graded_claims(stretch: Stretch, found: ExtractArgs, root: Path) -> list[Graded]:
    claims: list[Graded] = []
    for item in found["claims"]:
        kind: MemoryKind = item["kind"]
        basis, cites = grade(stretch, item["cites"], item["quotes"], kind == "outcome")
        text = item["text"].strip()
        files = known_files(stretch, root, item["files"])
        if kind == "observation" and basis == "user":
            files = widened(root, text, files)
        cited = evidence(stretch, cites)
        missing = unsupported(text, cited, files)
        claims.append(Graded(kind, text, files, cites, basis, cited, missing, tuple(item["restates"])))
    return claims


def decision_text(item: DecisionData) -> str:
    parts: list[str] = [item["chose"]]
    for option in item["options"]:
        parts.append(f"{option['option']}: {option['verdict']}")
    return " ".join(parts)


def graded_decisions(stretch: Stretch, found: ExtractArgs, root: Path) -> list[Weighed]:
    decisions: list[Weighed] = []
    for item in found["decisions"]:
        basis, cites = grade(stretch, item["cites"], item["quotes"], False)
        files = known_files(stretch, root, item["files"])
        cited = evidence(stretch, cites)
        decisions.append(Weighed(item, decided(basis), cites, files, cited, unsupported(decision_text(item), cited, files)))
    return decisions


def restated(store: Store, claim: Graded) -> list[Link]:
    found: list[Link] = []
    if claim.basis == "user":
        for ident in claim.restates:
            old = store.nodes.get(ident)
            if old is not None and old.kind == "observation" and old.basis == "user":
                found.append(Link("restates", old))
    return found


def carried(store: Store, every: Sequence[Stretch], stretch: Stretch) -> list[str]:
    found: list[str] = []
    for index, each in enumerate(every):
        if index > 0 and each.request.number == stretch.request.number:
            before = every[index - 1]
            changed = changed_paths(stretch)
            if len(changed) > 0 and overlaps(changed_paths(before), changed):
                ident = store.find("goal", before.asked.strip(), before.ref(before.request.number))
                if ident is not None and ident in store.nodes:
                    found.append(ident)
    return found


def continued(store: Store, idents: Sequence[str]) -> list[Link]:
    found: list[Link] = []
    for ident in idents:
        old = store.nodes.get(ident)
        if old is not None and old.kind == "goal":
            found.append(Link("continues", old))
    return found


def remember(store: Store, stretch: Stretch, found: ExtractArgs, root: Path) -> Remembered:
    made: list[Memory] = []
    dropped: list[str] = []
    request = stretch.request
    asked: Basis = "inferred"
    if isinstance(request.body, Request):
        asked = "user"
    ref = stretch.ref(request.number)
    goal = store.add("goal", stretch.asked.strip(), asked, ref, request.time, (), (), continued(store, found["continues"]), (), NONE)
    changed = changed_paths(stretch)
    decisions: list[Memory] = []
    for weighed in graded_decisions(stretch, found, root):
        item = weighed.item
        if len(weighed.missing) > 0:
            dropped.append(dropped_text("decision", item["chose"], weighed.missing))
        else:
            choices: list[Choice] = []
            for option in item["options"]:
                choices.append(Choice(f"{option['option']}: {option['verdict']}", option["taken"]))
            replaced = store.replacing(changed, item["replaces"], weighed.basis)
            step = cited_step(stretch, weighed.cites)
            conditions = Conditions(tuple(item["assumptions"]), tuple(item["revisit_when"]))
            source = stretch.ref(step.number)
            decision = store.decide(goal, item["chose"], choices, weighed.files, weighed.basis, source, step.time, replaced, weighed.cited, conditions)
            decisions.append(decision)
            made.append(decision)
    for claim in graded_claims(stretch, found, root):
        if len(claim.missing) > 0:
            dropped.append(dropped_text(claim.kind, claim.text, claim.missing))
        else:
            parent = goal
            for decision in decisions:
                if overlaps(decision.files, claim.files) or len(claim.files) == 0:
                    parent = decision
            step = cited_step(stretch, claim.cites)
            link = (Link("leads_to", parent),)
            source = stretch.ref(step.number)
            made.append(store.add(claim.kind, claim.text, claim.basis, source, step.time, claim.files, link, restated(store, claim), claim.cited, NONE))
    for pivot in pivots(stretch):
        cited = evidence(stretch, (pivot.failed, pivot.passed))
        step = cited_step(stretch, (pivot.failed,))
        link = (Link("leads_to", goal),)
        source = stretch.ref(pivot.failed)
        made.append(store.add("observation", pivot_text(pivot), "verified", source, step.time, (pivot.path,), link, (), cited, NONE))
    return Remembered(made, dropped)


def rejected(stretch: Stretch, found: ExtractArgs, root: Path) -> list[str]:
    reasons: list[str] = []
    for weighed in graded_decisions(stretch, found, root):
        if len(weighed.missing) > 0:
            reasons.append(dropped_text("decision", weighed.item["chose"], weighed.missing))
    for claim in graded_claims(stretch, found, root):
        if len(claim.missing) > 0:
            reasons.append(dropped_text(claim.kind, claim.text, claim.missing))
    return reasons


def misquotes(stretch: Stretch, found: ExtractArgs) -> list[str]:
    reasons: list[str] = []
    for item in found["decisions"]:
        for quote in misquoted(stretch, item["cites"], item["quotes"]):
            reasons.append(f'the decision "{item["chose"]}" quotes "{quote}", which is not in the steps it cites')
    for claim in found["claims"]:
        for quote in misquoted(stretch, claim["cites"], claim["quotes"]):
            reasons.append(f'the claim "{claim["text"]}" quotes "{quote}", which is not in the steps it cites')
    return reasons


def preview(found: Places, store: Store, session: str, number: int, extractor: Extractor) -> list[str] | Failure:
    for stretch in stretches(session, History(found.agent(session)), set(), True):
        if stretch.request.number == number:
            done = extractor.extract(stretch, record_of(stretch, store, extractor.settings))
            if isinstance(done, Failure):
                return done
            else:
                return list(done.dropped) + preview_lines(stretch, done.found, found.repo.root)
    return Failure(f"no stretch of {session} starts at step {number}")


def option_state(taken: bool) -> str:
    if taken:
        return "taken"
    else:
        return "dropped"


def decision_detail(item: DecisionData) -> list[str]:
    lines: list[str] = []
    for option in item["options"]:
        lines.append(f"  option ({option_state(option['taken'])}): {option['option']}: {option['verdict']}")
    for assumption in item["assumptions"]:
        lines.append(f"  assumes: {assumption}")
    for condition in item["revisit_when"]:
        lines.append(f"  revisit when: {condition}")
    return lines


def preview_lines(stretch: Stretch, found: ExtractArgs, root: Path) -> list[str]:
    lines: list[str] = []
    if len(found["continues"]) > 0:
        lines.append(f"continues the goals {', '.join(found['continues'])}")
    for reason in misquotes(stretch, found):
        lines.append(f"graded as inferred: {reason}")
    for weighed in graded_decisions(stretch, found, root):
        item = weighed.item
        if len(weighed.missing) > 0:
            lines.append(dropped_text("decision", item["chose"], weighed.missing))
        else:
            files = ", ".join(weighed.files)
            lines.append(f"decision ({weighed.basis}, cites {list(weighed.cites)}, files {files}, replaces {item['replaces']}): {item['chose']}")
            lines.extend(decision_detail(item))
    for claim in graded_claims(stretch, found, root):
        if len(claim.missing) > 0:
            lines.append(dropped_text(claim.kind, claim.text, claim.missing))
        else:
            restates = ""
            if len(claim.restates) > 0:
                restates = f", restates {', '.join(claim.restates)}"
            lines.append(f"{claim.kind} ({claim.basis}, cites {list(claim.cites)}, files {', '.join(claim.files)}{restates}): {claim.text}")
    for pivot in pivots(stretch):
        lines.append(f"observation (verified, found in the steps): {pivot_text(pivot)}")
    return lines


def extract_session(found_at: Places, store: Store, session: str, extractor: Extractor, final: bool, only: int | None, stop: threading.Event) -> Extracted:
    home = found_at.agent(session)
    with locked(home):
        state = read_state(home)
    made: list[Memory] = []
    dropped: list[str] = []
    failed: list[str] = []
    if state is not None:
        history = History(home)
        store.withdraw(session, live(history))
        todo: list[Stretch] = []
        for stretch in stretches(session, history, set(state["extracted"]), final):
            if only is None or stretch.request.number == only:
                todo.append(stretch)
        run = Run(found_at, store, session, extractor, stretches(session, history, set(), final), here())
        size = extractor.settings.parallel
        with ThreadPoolExecutor(max_workers=size) as pool:
            for first in range(0, len(todo), size):
                if not stop.is_set() and not run.halted:
                    batch = todo[first : first + size]
                    records: list[str] = []
                    with run.guard:
                        for stretch in batch:
                            records.append(record_of(stretch, store, extractor.settings))
                    list(pool.map(run.one, batch, records))
        made = run.made
        dropped = run.dropped
        failed = run.failed
    return Extracted(made, dropped, failed)


class Run:
    found: Places
    store: Store
    session: str
    extractor: Extractor
    every: list[Stretch]
    parent: str | None
    guard: threading.Lock
    made: list[Memory]
    dropped: list[str]
    failed: list[str]
    streak: int
    halted: bool

    def __init__(self, found: Places, store: Store, session: str, extractor: Extractor, every: list[Stretch], parent: str | None) -> None:
        self.found = found
        self.store = store
        self.session = session
        self.extractor = extractor
        self.every = every
        self.parent = parent
        self.guard = threading.Lock()
        self.made = []
        self.dropped = []
        self.failed = []
        self.streak = 0
        self.halted = False

    def one(self, stretch: Stretch, record: str) -> None:
        ref = stretch.ref(stretch.request.number)
        given: dict[str, JSON] = {"asked": cut(stretch.asked, CUT), "steps": len(stretch.steps), "record": len(record)}
        with span("stretch", ref, str(self.found.repo.root), self.parent, given) as doing:
            done = self.extractor.extract(stretch, record)
            with self.guard:
                if isinstance(done, Failure):
                    doing.ok = False
                    doing.attrs["reason"] = cut(done.reason, CUT)
                    self.failing(f"{ref}: {done.reason}")
                else:
                    self.streak = 0
                    self.kept(stretch, done, doing)

    def failing(self, reason: str) -> None:
        self.failed.append(reason)
        self.streak += 1
        if self.streak >= self.extractor.settings.failure_streak and not self.halted:
            summary = f"extraction of {self.session} stopped after {self.streak} stretches in a row failed; the last said: {reason}"
            noticed("extract", str(self.found.repo.root), summary, "\n".join(self.failed[-self.streak :]))
            self.failed.append(summary)
            self.halted = True

    def kept(self, stretch: Stretch, done: Extraction, doing: Span) -> None:
        home = self.found.agent(self.session)
        with locked(home):
            latest = read_state(home)
            if latest is not None and stretch.request.number not in latest["extracted"]:
                self.remembered(stretch, done, doing)
                latest["extracted"].append(stretch.request.number)
                write_state(home, latest)
            else:
                doing.ok = False
                doing.attrs["reason"] = "another process extracted it first, so this extraction is not kept"
                noticed("extract", str(self.found.repo.root), f"two processes extracted the same stretch of {self.session}", stretch.ref(stretch.request.number))

    def remembered(self, stretch: Stretch, done: Extraction, doing: Span) -> None:
        found = done.found
        for ident in carried(self.store, self.every, stretch):
            if ident not in found["continues"]:
                found["continues"].append(ident)
        kept = remember(self.store, stretch, found, self.found.repo.root)
        self.made.extend(kept.made)
        left = [*done.dropped, *kept.dropped]
        for reason in misquotes(stretch, found):
            left.append(f"graded as inferred: {reason}")
        self.dropped.extend(left)
        for memory in kept.made:
            chunk(doing, "note", f"{memory.kind} ({memory.basis}) {memory.id}: {memory.text}")
        for reason in left:
            chunk(doing, "note", f"dropped: {reason}")
        doing.attrs["remembered"] = len(kept.made)
        doing.attrs["dropped"] = len(left)
