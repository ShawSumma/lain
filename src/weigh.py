from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

from config import SETTINGS
from extract import earlier, facts, fitted, prompt_name, record_of, stretches
from history import History
from ingest import agent_names
from memory import Store
from places import places
from prompts import prompt
from spec import EXTRACT, schemas


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="weigh.py", description="Say how many characters each extraction prompt would send, without calling a model.")
    parser.add_argument("project", help="any directory in the project's checkout")
    parser.add_argument("--agent", default="", help="only this history, such as git")
    given = parser.parse_args(argv)
    found = places(Path(given.project).resolve())
    store = Store(found.memory)
    tools = len(json.dumps(schemas((EXTRACT,))))
    began = time.monotonic()
    sizes: list[int] = []
    parts: dict[str, list[int]] = {"system": [], "tools": [], "steps": [], "facts": [], "earlier": []}
    for name in agent_names(found):
        if given.agent in ("", name):
            system = len(prompt(prompt_name(name), root="."))
            for stretch in stretches(name, History(found.agent(name)), set(), True):
                sizes.append(system + tools + len(record_of(stretch, store, SETTINGS)))
                parts["system"].append(system)
                parts["tools"].append(tools)
                parts["steps"].append(len(fitted(stretch, SETTINGS)))
                parts["facts"].append(len(facts(stretch)))
                parts["earlier"].append(len(earlier(store, stretch)))
    seconds = time.monotonic() - began
    if len(sizes) == 0:
        print("no stretches")
    else:
        print(f"{len(sizes)} prompts, {sum(sizes)} characters in all, median {statistics.median(sizes):.0f}, largest {max(sizes)}, built in {seconds:.1f}s")
        for part, each in parts.items():
            print(f"  {part}: median {statistics.median(each):.0f}, largest {max(each)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
