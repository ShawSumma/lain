from __future__ import annotations

import time
import traceback
from pathlib import Path
from typing import Literal, TypedDict

from page import Builder, Page, Unreadable, moment, named, number, numbered, pages, read, real, when, write
from places import INSTALL
from redact import redacted
from spans import cut
from store import locked

BUGS = INSTALL / "bugs"
HERE = Path(__file__).resolve().parent
DEPTH = 3

type Origin = Literal["caught", "noticed", "reported"]


class KnownData(TypedDict):
    number: int
    signature: str
    origin: Origin
    summary: str
    count: int
    first: float
    last: float
    wheres: list[str]
    projects: list[str]
    detail: str


class BugsData(TypedDict):
    bugs: list[KnownData]


class ReportedData(TypedDict):
    number: int


told: set[str] = set()


def origin_of(text: str) -> Origin:
    match text:
        case "caught" | "noticed" | "reported":
            return text
        case _:
            raise Unreadable(f"'{text}' is not where a bug came from")


def bug_page(bug: KnownData) -> Page:
    built = Builder(f"bug {bug['number']}: {bug['summary']}")
    built.field("signature", bug["signature"])
    built.field("origin", bug["origin"])
    built.field("count", str(bug["count"]))
    built.field("first", when(bug["first"]))
    built.field("last", when(bug["last"]))
    for where in bug["wheres"]:
        built.field("where", where)
    for project in bug["projects"]:
        built.field("project", project)
    built.section("summary", bug["summary"])
    built.section("detail", bug["detail"])
    return built.page()


def bug_of(path: Path) -> KnownData:
    page = read(path)
    return {
        "number": numbered(path)[0],
        "signature": page.value("signature"),
        "origin": origin_of(page.need("origin")),
        "summary": page.value("summary"),
        "count": number(page.need("count")),
        "first": real(page.need("first")),
        "last": real(page.need("last")),
        "wheres": page.every("where"),
        "projects": page.every("project"),
        "detail": page.value("detail"),
    }


def kept(origin: Origin, where: str, project: str, summary: str, detail: str, signature: str) -> int:
    now = time.time()
    with locked(BUGS):
        for path in pages(BUGS):
            bug = bug_of(path)
            if bug["signature"] == signature:
                bug["count"] += 1
                bug["last"] = now
                bug["summary"] = redacted(summary)
                bug["detail"] = redacted(detail)
                if where not in bug["wheres"]:
                    bug["wheres"].append(where)
                if project != "" and project not in bug["projects"]:
                    bug["projects"].append(project)
                write(path, bug_page(bug))
                return bug["number"]
        made = len(pages(BUGS)) + 1
        projects: list[str] = []
        if project != "":
            projects.append(project)
        bug: KnownData = {
            "number": made,
            "signature": signature,
            "origin": origin,
            "summary": redacted(summary),
            "count": 1,
            "first": now,
            "last": now,
            "wheres": [where],
            "projects": projects,
            "detail": redacted(detail),
        }
        write(BUGS / named(made, summary), bug_page(bug))
        return made


def signature_of(error: BaseException) -> str:
    frames: list[str] = []
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(frame.filename)
        if path.parent == HERE:
            frames.append(f"{path.name} {frame.name}")
    return " from ".join([type(error).__name__, *reversed(frames[-DEPTH:])])


def caught(error: BaseException, where: str, project: str, context: str) -> str:
    summary = traceback.format_exception_only(error)[-1].strip()
    detail = "".join(traceback.format_exception(error))
    if context != "":
        detail = f"{cut(context)}\n\n{detail}"
    made = kept("caught", where, project, summary, detail, signature_of(error))
    return f"{summary} (recorded as lain bug {made})"


def noticed(where: str, project: str, summary: str, detail: str) -> None:
    signature = f"{where}: {summary}"
    if signature not in told:
        told.add(signature)
        kept("noticed", where, project, summary, cut(detail), signature)


def reported(text: str, project: str) -> int:
    return kept("reported", "report", project, text.partition("\n")[0], text, text.partition("\n")[0])


def known() -> list[KnownData]:
    found: list[KnownData] = []
    for path in pages(BUGS):
        try:
            found.append(bug_of(path))
        except Unreadable:
            pass
    return sorted(found, key=lambda each: -each["last"])


def known_line(each: KnownData) -> str:
    seen = f"once at {moment(each['last'])}"
    if each["count"] > 1:
        seen = f"{each['count']} times from {moment(each['first'])} to {moment(each['last'])}"
    about = ", ".join(each["wheres"])
    if len(each["projects"]) > 0:
        names: list[str] = []
        for project in each["projects"]:
            names.append(Path(project).name)
        about = f"{about}, for {', '.join(names)}"
    return f"bug {each['number']} {each['origin']} {seen}, in {about}: {each['summary']}"


def known_lines(found: list[KnownData]) -> list[str]:
    lines: list[str] = []
    for each in found:
        lines.append(known_line(each))
    if len(lines) == 0:
        lines.append("lain has recorded no bugs")
    return lines
