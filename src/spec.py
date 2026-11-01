from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, Required, TypedDict

from prompts import tool_text
from store import JSON

type Shape = Literal["text", "number", "real", "flag", "texts", "numbers", "records", "record"]


class OptionData(TypedDict):
    option: str
    verdict: str
    taken: bool


class MatchData(TypedDict):
    line: int
    text: str
    why: str


class MatchesArgs(TypedDict):
    matches: list[MatchData]


class PathArgs(TypedDict):
    path: str


class CommandArgs(TypedDict):
    command: str


class ChoiceData(TypedDict):
    chose: str
    options: list[OptionData]
    files: list[str]
    evidence: list[str]


class FactData(TypedDict):
    kind: Literal["outcome", "observation"]
    text: str
    files: list[str]
    evidence: list[str]


class CommitArgs(TypedDict):
    message: str
    trivial: str
    decisions: list[ChoiceData]
    facts: list[FactData]


class DecisionData(TypedDict):
    chose: str
    options: list[OptionData]
    files: list[str]
    cites: list[int]
    quotes: list[str]
    replaces: list[str]
    assumptions: list[str]
    revisit_when: list[str]


class ClaimData(TypedDict):
    kind: Literal["outcome", "observation"]
    text: str
    files: list[str]
    cites: list[int]
    quotes: list[str]
    restates: list[str]


class ContradictionData(TypedDict):
    item: int
    why: str


class ContradictedArgs(TypedDict):
    contradicted: list[ContradictionData]


class ExtractArgs(TypedDict):
    decisions: list[DecisionData]
    claims: list[ClaimData]
    continues: list[str]


class RecheckArgs(TypedDict):
    verdict: Literal["holds", "outdated", "changed"]
    text: str
    files: list[str]
    reason: str


class DescribeArgs(TypedDict):
    summary: str
    text: str


class Schema(TypedDict, total=False):
    type: Required[str]
    description: str
    enum: list[str]
    items: Schema
    properties: dict[str, Schema]
    required: list[str]
    additionalProperties: bool


class Function(TypedDict):
    name: str
    description: str
    strict: bool
    parameters: Schema


class ToolSchema(TypedDict):
    type: str
    function: Function


@dataclass(frozen=True)
class Field:
    name: str
    shape: Shape
    fields: tuple[Field, ...] = ()
    choices: tuple[str, ...] = ()
    owner: str = ""


@dataclass(frozen=True)
class Tool:
    name: str
    fields: tuple[Field, ...]


PATH = Field("path", "text")
PATHS = Field("paths", "texts")
LIST_DIR = Tool("list_dir", (PATH,))
READ_FILE = Tool("read_file", (PATH,))
SEARCH = Tool("search", (Field("pattern", "text"), PATHS))
FIND = Tool("find", (Field("question", "text"), PATHS))
BEFORE = Tool("before", (PATH,))
COMMAND = Tool("command", (Field("command", "text"),))
OPTIONS = Field("options", "records", (Field("option", "text"), Field("verdict", "text"), Field("taken", "flag")))
COMMIT_NOTES = Tool(
    "commit_notes",
    (
        Field("message", "text"),
        Field("trivial", "text"),
        Field("decisions", "records", (Field("chose", "text"), OPTIONS, Field("files", "texts"), Field("evidence", "texts"))),
        Field("facts", "records", (Field("kind", "text", choices=("outcome", "observation")), Field("text", "text"), Field("files", "texts"), Field("evidence", "texts"))),
    ),
)
DECISIONS = (
    Field("chose", "text"),
    Field("options", "records", (Field("option", "text"), Field("verdict", "text"), Field("taken", "flag"))),
    Field("files", "texts"),
    Field("cites", "numbers"),
    Field("quotes", "texts"),
    Field("replaces", "texts"),
    Field("assumptions", "texts"),
    Field("revisit_when", "texts"),
)
CLAIMS = (
    Field("kind", "text", choices=("outcome", "observation")),
    Field("text", "text"),
    Field("files", "texts"),
    Field("cites", "numbers"),
    Field("quotes", "texts"),
    Field("restates", "texts"),
)
EXTRACT = Tool("extract", (Field("decisions", "records", DECISIONS), Field("claims", "records", CLAIMS), Field("continues", "texts")))
CONTRADICTED = Tool("contradicted", (Field("contradicted", "records", (Field("item", "number"), Field("why", "text"))),))
RECHECK = Tool(
    "recheck",
    (Field("verdict", "text", choices=("holds", "outdated", "changed")), Field("text", "text"), Field("files", "texts"), Field("reason", "text")),
)
DESCRIBE = Tool("describe", (Field("summary", "text"), Field("text", "text")))
MATCHES = Tool(
    "matches",
    (Field("matches", "records", (Field("line", "number"), Field("text", "text"), Field("why", "text"))),),
)


def object_schema(owner: str, fields: Sequence[Field]) -> Schema:
    properties: dict[str, Schema] = {}
    required: list[str] = []
    for field in fields:
        properties[field.name] = field_schema(owner, field)
        required.append(field.name)
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


def text_schema(key: str, field: Field) -> Schema:
    text: Schema = {"type": "string", "description": tool_text(key)}
    if len(field.choices) > 0:
        text["enum"] = list(field.choices)
    return text


def field_schema(owner: str, field: Field) -> Schema:
    key = f"{field.owner or owner}.{field.name}"
    match field.shape:
        case "text":
            return text_schema(key, field)
        case "number":
            return {"type": "integer", "description": tool_text(key)}
        case "real":
            return {"type": "number", "description": tool_text(key)}
        case "flag":
            return {"type": "boolean", "description": tool_text(key)}
        case "texts":
            return {"type": "array", "items": {"type": "string"}, "description": tool_text(key)}
        case "numbers":
            return {"type": "array", "items": {"type": "integer"}, "description": tool_text(key)}
        case _:
            return {"type": "array", "items": object_schema(key, field.fields), "description": tool_text(key)}


def schema(tool: Tool) -> ToolSchema:
    function: Function = {
        "name": tool.name,
        "description": tool_text(tool.name),
        "strict": True,
        "parameters": parameters(tool),
    }
    return {"type": "function", "function": function}


def parameters(tool: Tool) -> Schema:
    return object_schema(tool.name, tool.fields)


def schemas(tools: Sequence[Tool]) -> list[ToolSchema]:
    found: list[ToolSchema] = []
    for tool in tools:
        found.append(schema(tool))
    return found


def names(tools: Sequence[Tool]) -> list[str]:
    found: list[str] = []
    for tool in tools:
        found.append(tool.name)
    return found


def items_wrong(field: Field, value: JSON, place: str) -> str | None:
    if not isinstance(value, list):
        return f"{place} is not a list"
    else:
        for index, item in enumerate(value):
            found = shape_wrong(field, item, f"{place}[{index}]")
            if found is not None:
                return found
        return None


def shape_wrong(field: Field, value: JSON, place: str) -> str | None:
    match field.shape:
        case "text" if not isinstance(value, str):
            return f"{place} is not a string"
        case "text" if len(field.choices) > 0 and value not in field.choices:
            return f"{place} is not one of {', '.join(field.choices)}"
        case "number" if not isinstance(value, int) or isinstance(value, bool):
            return f"{place} is not an integer"
        case "real" if not isinstance(value, int | float) or isinstance(value, bool):
            return f"{place} is not a number"
        case "flag" if not isinstance(value, bool):
            return f"{place} is not true or false"
        case "texts":
            return items_wrong(Field(field.name, "text"), value, place)
        case "numbers":
            return items_wrong(Field(field.name, "number"), value, place)
        case "records":
            return items_wrong(Field(field.name, "record", field.fields), value, place)
        case "record":
            return fields_wrong(field.fields, value, place)
        case _:
            return None


def fields_wrong(fields: Sequence[Field], value: JSON, place: str) -> str | None:
    if not isinstance(value, dict):
        return f"{place} is not an object"
    else:
        for field in fields:
            inner = f"{place}.{field.name}"
            if field.name not in value:
                return f"{inner} is missing"
            else:
                found = shape_wrong(field, value[field.name], inner)
                if found is not None:
                    return found
        return None


def misfit(tools: Sequence[Tool], tool: str, arguments: str) -> str | None:
    for known in tools:
        if known.name == tool:
            try:
                given: JSON = json.loads(arguments)
            except json.JSONDecodeError as error:
                before = arguments[max(0, error.pos - 40) : error.pos]
                after = arguments[error.pos : error.pos + 40]
                return f"the arguments are not valid JSON: {error.msg}, where it breaks off at {json.dumps(before)} and goes on {json.dumps(after)}"
            else:
                return fields_wrong(known.fields, given, "arguments")
    return f"there is no tool called {tool}"
