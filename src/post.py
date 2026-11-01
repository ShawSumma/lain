from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from page import Builder, Page, Unreadable, flag, flagged, named, number, numbered, pages, read, real, when, write
from store import locked

POST = "post"
ADDRESSES = "addresses"
LETTERS = "letters"
NAME = re.compile(r"[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9_-]+)*")


class LetterData(TypedDict):
    number: int
    sender: str
    to: str
    text: str
    time: float
    answers: int | None
    read: bool


@dataclass(frozen=True)
class Address:
    number: int
    name: str


def address_of(project: str, agent: str) -> str:
    parts: list[str] = [project.replace(".", "-")]
    for part in agent.split("/"):
        if part != "":
            parts.append(part.replace(".", "-"))
    return ".".join(parts).lower()


def matching(known: list[Address], wanted: Callable[[Address], bool]) -> list[Address]:
    found: list[Address] = []
    for each in known:
        if wanted(each):
            found.append(each)
    return found


def letter_page(letter: LetterData) -> Page:
    built = Builder(f"letter {letter['number']} from {letter['sender']} to {letter['to']}")
    built.field("from", letter["sender"])
    built.field("to", letter["to"])
    built.field("time", when(letter["time"]))
    if letter["answers"] is not None:
        built.field("answers", str(letter["answers"]))
    built.field("read", flag(letter["read"]))
    built.section("text", letter["text"])
    return built.page()


def letter_of(path: Path) -> LetterData:
    page = read(path)
    answers: int | None = None
    held = page.one("answers")
    if held is not None:
        answers = number(held)
    return {
        "number": numbered(path)[0],
        "sender": page.need("from"),
        "to": page.need("to"),
        "text": page.value("text"),
        "time": real(page.need("time")),
        "answers": answers,
        "read": flagged(page.need("read")),
    }


class Office:
    folder: Path

    def __init__(self, home: Path) -> None:
        self.folder = home / POST

    def names(self) -> list[Address]:
        found: list[Address] = []
        for path in pages(self.folder / ADDRESSES):
            found.append(Address(numbered(path)[0], read(path).need("name")))
        return found

    def register(self, name: str) -> Address | str:
        if NAME.fullmatch(name) is None:
            return f"{name} is not a name: lowercase letters, digits, dashes and underscores, in parts joined by dots"
        else:
            with locked(self.folder):
                known = self.names()
                for each in known:
                    if each.name == name:
                        return each
                made = Address(len(known) + 1, name)
                write(self.folder / ADDRESSES / f"{made.number}.md", Page(f"address {made.number}: {name}", (("name", name),), ()))
                return made

    def resolve(self, to: str) -> list[Address] | str:
        known = self.names()
        found: list[Address] = []
        if to.startswith("#") and to[1:].isdigit():
            found = matching(known, lambda each: each.number == int(to[1:]))
        elif to.endswith(".*"):
            found = matching(known, lambda each: each.name.startswith(to.removesuffix("*")))
        else:
            found = matching(known, lambda each: each.name == to)
        if len(found) == 0:
            return f"no one is addressed as {to}; `who` lists the addresses"
        else:
            return found

    def letters(self) -> list[tuple[Path, LetterData]]:
        found: list[tuple[Path, LetterData]] = []
        for path in pages(self.folder / LETTERS):
            try:
                found.append((path, letter_of(path)))
            except Unreadable:
                pass
        return found

    def send(self, sender: str, to: str, text: str, answers: int | None) -> list[LetterData] | str:
        me = self.register(sender)
        targets = self.resolve(to)
        if isinstance(me, str):
            return me
        elif isinstance(targets, str):
            return targets
        else:
            return self.sent(me, targets, text, answers)

    def sent(self, me: Address, targets: list[Address], text: str, answers: int | None) -> list[LetterData]:
        found: list[LetterData] = []
        with locked(self.folder):
            count = len(pages(self.folder / LETTERS))
            for target in targets:
                if target.name != me.name:
                    count += 1
                    letter: LetterData = {"number": count, "sender": me.name, "to": target.name, "text": text, "time": time.time(), "answers": answers, "read": False}
                    write(self.folder / LETTERS / named(count, text.partition("\n")[0]), letter_page(letter))
                    found.append(letter)
        return found

    def take(self, name: str) -> list[LetterData]:
        found: list[LetterData] = []
        with locked(self.folder):
            for path, letter in self.letters():
                if letter["to"] == name and not letter["read"]:
                    letter["read"] = True
                    write(path, letter_page(letter))
                    found.append(letter)
        return found


def shown(letter: LetterData, numbers: dict[str, int]) -> str:
    sender = f"#{numbers.get(letter['sender'], 0)} {letter['sender']}"
    answers = ""
    if letter["answers"] is not None:
        answers = f", answering letter {letter['answers']}"
    return f"letter {letter['number']} from {sender}{answers}:\n{letter['text']}"


def address_lines(office: Office) -> list[str]:
    found: list[str] = []
    for each in office.names():
        found.append(f"#{each.number} {each.name}")
    return found


def sent_list(letters: list[LetterData]) -> str:
    found: list[str] = []
    for letter in letters:
        found.append(f"{letter['number']} to {letter['to']}")
    return ", ".join(found)


def delivered(office: Office, name: str) -> list[str]:
    numbers: dict[str, int] = {}
    for each in office.names():
        numbers[each.name] = each.number
    found: list[str] = []
    for letter in office.take(name):
        found.append(shown(letter, numbers))
    return found
