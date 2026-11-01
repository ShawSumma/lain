from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypedDict

from config import Settings
from page import Builder, dated, moment, pages, read, real, when, write
from places import Places
from stale import Flagged, Rechecked

UPKEEP = "upkeep"

type Pick = Callable[[list[Flagged]], list[Flagged]]


class RunData(TypedDict):
    time: float
    tried: list[str]
    failed: list[str]


def upkeeps(found: Places) -> list[RunData]:
    done: list[RunData] = []
    for path in sorted((found.home / UPKEEP).glob("*.md")):
        page = read(path)
        done.append({"time": real(page.need("time")), "tried": page.every("tried"), "failed": page.every("failed")})
    return done


def picker(done: list[RunData], limit: int) -> Pick:
    last: dict[str, float] = {}
    for run in done:
        for ident in run["tried"]:
            last[ident] = run["time"]

    def pick(flagged: list[Flagged]) -> list[Flagged]:
        waiting: list[Flagged] = []
        for each in flagged:
            if each.memory.basis != "user":
                waiting.append(each)
        waiting.sort(key=lambda each: (last.get(each.memory.id, 0.0), -each.memory.time))
        return waiting[:limit]

    return pick


def due(found: Places, settings: Settings) -> Pick | None:
    done = upkeeps(found)
    if settings.upkeep_rechecks == 0:
        return None
    elif len(done) > 0 and time.time() - done[-1]["time"] < settings.upkeep_every:
        return None
    else:
        return picker(done, settings.upkeep_rechecks)


def kept(found: Places, started: float, result: Rechecked) -> None:
    built = Builder(f"upkeep at {moment(started)}")
    built.field("time", when(started))
    for ident in result.tried:
        built.field("tried", ident)
    for ident in result.failed:
        built.field("failed", ident)
    write(found.home / UPKEEP / f"{dated(started)}.md", built.page())
