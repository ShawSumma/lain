from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from model import Answer, Client, Message, Usage
from prompts import message, prompt
from spec import MATCHES, MatchesArgs, misfit
from tree import files_under, normal


@dataclass(frozen=True)
class Job:
    path: str
    text: str
    question: str


@dataclass(frozen=True)
class Found:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class Unsearched:
    path: str
    reason: str


@dataclass(frozen=True)
class Outside:
    path: str


def numbered(text: str) -> str:
    lines: list[str] = []
    number = 0
    for line in text.splitlines():
        number += 1
        lines.append(f"{number}| {line}")
    return "\n".join(lines)


def readable(root: Path, path: str) -> str | None:
    data = (root / path).read_bytes()
    if b"\x00" in data:
        return None
    else:
        return data.decode(errors="replace")


def listing(root: Path, raw: Sequence[str]) -> list[str] | Outside:
    paths: list[str] = []
    for item in raw:
        clean = normal(item)
        if clean is None:
            return Outside(item)
        else:
            for path in files_under(root, clean):
                if path not in paths:
                    paths.append(path)
    return paths


def lines_of(path: str, answer: Answer) -> Found:
    lines: list[str] = []
    for call in answer.calls:
        args: MatchesArgs = json.loads(call.arguments)
        for item in args["matches"]:
            lines.append(f"{path}:{item['line']}: {item['text'].strip()} ({item['why']})")
    return Found(tuple(lines))


class Finder:
    usage: Usage
    settings: Settings

    def __init__(self, usage: Usage, settings: Settings) -> None:
        self.usage = usage
        self.settings = settings

    def one(self, root: Path, job: Job) -> Found | Unsearched:
        sent: list[Message] = [
            {"role": "system", "content": prompt("find", question=job.question, path=job.path)},
            {"role": "user", "content": numbered(job.text)},
        ]
        answer = Client(self.usage, self.settings, job.path, root).chat(sent, (MATCHES,), self.settings.light_effort)
        match answer:
            case Answer():
                for call in answer.calls:
                    problem = misfit((MATCHES,), call.tool, call.arguments)
                    if problem is not None:
                        return Unsearched(job.path, problem)
                return lines_of(job.path, answer)
            case _:
                return Unsearched(job.path, answer.reason)

    def find(self, root: Path, question: str, raw: Sequence[str]) -> str:
        listed = listing(root, raw)
        match listed:
            case Outside(path):
                return message("outside", path=path)
            case _:
                return self.search(root, question, listed)

    def search(self, root: Path, question: str, paths: Sequence[str]) -> str:
        jobs: list[Job] = []
        for path in paths:
            text = readable(root, path)
            if text is not None:
                jobs.append(Job(path, text, question))
        if len(jobs) == 0:
            return message("find_nothing")
        else:
            results: list[Found | Unsearched] = []
            for job in jobs:
                results.append(self.one(root, job))
            return self.summary(results, len(jobs))

    def summary(self, results: Sequence[Found | Unsearched], searched: int) -> str:
        lines: list[str] = []
        for result in results:
            match result:
                case Found(found):
                    lines.extend(found)
                case _:
                    lines.append(message("find_failed", path=result.path, error=result.reason))
        if len(lines) == 0:
            return message("find_none", count=str(searched))
        else:
            return "\n".join(lines)
