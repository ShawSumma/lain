from __future__ import annotations

from history import Accepted, Result, Step
from tree import Change, section_paths


def patch_of(step: Step) -> tuple[Change, ...]:
    match step.body:
        case Result(patch=patch):
            return patch
        case Accepted(patch=patch):
            return patch
        case _:
            return ()


def paths_changed(step: Step) -> list[str]:
    match step.body:
        case Result(tool="diff" | "rename", text=text):
            return section_paths(text)
        case _:
            found: list[str] = []
            for change in patch_of(step):
                found.append(change.path)
            return found
