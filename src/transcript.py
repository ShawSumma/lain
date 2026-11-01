from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from store import JSON, records


class Block(TypedDict, total=False):
    type: str
    text: str
    thinking: str
    id: str
    name: str
    input: dict[str, JSON]
    tool_use_id: str
    content: str | list[Block]
    is_error: bool


class UsageData(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int


class Message(TypedDict, total=False):
    id: str
    role: str
    model: str
    content: str | list[Block]
    stop_reason: str | None
    usage: UsageData


class Origin(TypedDict, total=False):
    kind: str


class Record(TypedDict, total=False):
    type: str
    subtype: str
    uuid: str
    parentUuid: str | None
    logicalParentUuid: str | None
    isSidechain: bool
    isMeta: bool
    isCompactSummary: bool
    timestamp: str
    cwd: str
    gitBranch: str
    sessionId: str
    agentId: str
    message: Message
    toolUseResult: JSON
    origin: Origin
    promptSource: str
    content: str


@dataclass(frozen=True)
class Transcript:
    path: Path
    records: list[Record]


def load(path: Path) -> Transcript:
    found: list[Record] = []
    for data in records(path):
        found.append(cast(Record, data))
    return Transcript(path, found)


def blocks(message: Message) -> list[Block]:
    content = message.get("content", [])
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    else:
        return content


def keys_of(value: JSON) -> str:
    if isinstance(value, dict):
        return ",".join(sorted(value))
    else:
        return type(value).__name__


def survey(transcript: Transcript) -> list[str]:
    kinds: Counter[str] = Counter()
    shapes: Counter[str] = Counter()
    parts: Counter[str] = Counter()
    results: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    tools: dict[str, str] = {}
    for record in transcript.records:
        kind = record.get("type", "?")
        subtype = record.get("subtype")
        if subtype is not None:
            kind = f"{kind}/{subtype}"
        kinds[kind] += 1
        shapes[f"{kind}: {keys_of(cast(JSON, record))}"] += 1
        message = record.get("message")
        if message is not None:
            if kind == "user" and isinstance(message.get("content"), str):
                origin = record.get("origin", {}).get("kind")
                origins[f"origin {origin}, promptSource {record.get('promptSource')}"] += 1
            for block in blocks(message):
                parts[f"{kind} {block.get('type', '?')}"] += 1
                if block.get("type") == "tool_use":
                    tools[block.get("id", "")] = block.get("name", "?")
                elif block.get("type") == "tool_result" and "toolUseResult" in record:
                    name = tools.get(block.get("tool_use_id", ""), "?")
                    results[f"{name}: {keys_of(record['toolUseResult'])}"] += 1
    lines = [f"{transcript.path}: {len(transcript.records)} records"]
    for title, counts in (("records", kinds), ("shapes", shapes), ("content", parts), ("tool results", results), ("user text", origins)):
        lines.append(f"{title}:")
        for key, count in counts.most_common():
            lines.append(f"  {count:6} {key}")
    lines.append("records whose parent is not before them in the file:")
    lines.extend(orphans(transcript))
    return lines


def orphans(transcript: Transcript) -> list[str]:
    known: dict[str, int] = {}
    for number, record in enumerate(transcript.records, 1):
        uuid = record.get("uuid")
        if uuid is not None:
            known[uuid] = number
    seen: set[str] = set()
    lines: list[str] = []
    for number, record in enumerate(transcript.records, 1):
        parent = record.get("parentUuid")
        if parent is None:
            parent = record.get("logicalParentUuid")
        where = ""
        if parent is not None and parent not in known:
            where = "nowhere"
        elif parent is not None and parent not in seen:
            later = transcript.records[known[parent] - 1]
            where = f"later, at line {known[parent]}: {later.get('type')}/{later.get('subtype')} at {later.get('timestamp')}"
        if where != "":
            kind = record.get("type", "?")
            lines.append(f"  line {number}: {kind}/{record.get('subtype')} {record.get('uuid')} at {record.get('timestamp')}, parent {parent} {where}")
        uuid = record.get("uuid")
        if uuid is not None:
            seen.add(uuid)
    return lines


def main(argv: list[str]) -> int:
    for raw in argv:
        for line in survey(load(Path(raw))):
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
