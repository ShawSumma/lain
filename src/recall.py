from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TypedDict, cast

from memory import Store
from places import places
from story import Library


class RankCase(TypedDict):
    query: str
    expect: list[str]


class UpdateCase(TypedDict):
    query: str
    newer: str
    older: str


class AbsentCase(TypedDict):
    query: str
    missing: list[str]


type Case = RankCase | UpdateCase | AbsentCase


def stretches_found(library: Library, query: str) -> list[str]:
    refs: list[str] = []
    for hit in library.search(query)["hits"]:
        ref = hit["ref"]
        if hit["kind"] == "memory":
            holding = library.stretch_holding(ref)
            if holding is not None:
                ref = holding
        if ref not in refs:
            refs.append(ref)
    return refs


def hits_found(library: Library, query: str) -> list[str]:
    refs: list[str] = []
    for hit in library.search(query)["hits"]:
        refs.append(hit["ref"])
    return refs


def ranked(library: Library, cases: list[RankCase]) -> None:
    total = 0.0
    count = 0
    for case in cases:
        refs = stretches_found(library, case["query"])
        for ref in case["expect"]:
            count += 1
            if ref in refs:
                total += 1 / (refs.index(ref) + 1)
                print(f"rank {refs.index(ref) + 1:<3} {ref} for {case['query']!r}")
            else:
                print(f"missed  {ref} for {case['query']!r}")
    if count > 0:
        print(f"mean reciprocal rank {total / count:.3f} over {count} expected results")


def updated(library: Library, cases: list[UpdateCase]) -> None:
    passed = 0
    for case in cases:
        refs = hits_found(library, case["query"])
        newer = case["newer"]
        older = case["older"]
        if newer in refs and (older not in refs or refs.index(newer) < refs.index(older)):
            passed += 1
            print(f"newer first  {newer} over {older} for {case['query']!r}")
        elif newer in refs:
            print(f"older first  {older} over {newer} for {case['query']!r}")
        else:
            print(f"missed       {newer} for {case['query']!r}")
    print(f"the newer decision came first for {passed} of {len(cases)} queries")


def abstained(library: Library, cases: list[AbsentCase]) -> None:
    passed = 0
    for case in cases:
        found = library.search(case["query"])
        said: list[str] = []
        for word in case["missing"]:
            if word not in found["unmatched"]:
                said.append(word)
        if len(said) == 0:
            passed += 1
            print(f"said nothing mentions {', '.join(case['missing'])} for {case['query']!r}")
        else:
            print(f"found {', '.join(said)} recorded for {case['query']!r}")
    print(f"the unrecorded topic was named for {passed} of {len(cases)} queries")


def measured(project: Path, cases: list[Case]) -> int:
    at = places(project)
    library = Library(at, Store(at.memory))
    ranks: list[RankCase] = []
    updates: list[UpdateCase] = []
    absent: list[AbsentCase] = []
    for case in cases:
        match case:
            case {"expect": list()}:
                ranks.append(cast(RankCase, case))
            case {"newer": str()}:
                updates.append(cast(UpdateCase, case))
            case _:
                absent.append(cast(AbsentCase, case))
    if len(ranks) > 0:
        ranked(library, ranks)
    if len(updates) > 0:
        updated(library, updates)
    if len(absent) > 0:
        abstained(library, absent)
    return 0


def main(argv: list[str]) -> int:
    match argv:
        case [project, cases] if Path(project).is_dir() and Path(cases).is_file():
            return measured(Path(project).resolve(), json.loads(Path(cases).read_text()))
        case _:
            print("recall.py PROJECT CASES: how well search finds what the cases in CASES, a JSON list such as bench/recall.json, expect", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
