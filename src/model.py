from __future__ import annotations

import http.client
import json
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, NotRequired, Required, TypedDict
from pathlib import Path
from urllib.parse import urlsplit

from bugs import noticed
from config import Effort, Settings
from history import Call, Tokens
from keys import PROVIDER_KEYS
from spans import Span, chunk, command_line, cut, here, project_of, span
from spec import Tool, ToolSchema, schemas
from store import JSON

TRANSIENT = (408, 429, 500, 502, 503, 504)


class FunctionCall(TypedDict):
    name: str
    arguments: str


class ToolCallData(TypedDict):
    id: str
    type: Literal["function"]
    function: FunctionCall


class SystemMessage(TypedDict):
    role: Literal["system"]
    content: str


class UserMessage(TypedDict):
    role: Literal["user"]
    content: str


class AssistantMessage(TypedDict):
    role: Literal["assistant"]
    content: str
    reasoning_content: NotRequired[str]
    tool_calls: NotRequired[list[ToolCallData]]


class ToolMessage(TypedDict):
    role: Literal["tool"]
    tool_call_id: str
    content: str


type Message = SystemMessage | UserMessage | AssistantMessage | ToolMessage


class StreamOptions(TypedDict):
    include_usage: bool


class Reasoning(TypedDict, total=False):
    effort: Effort
    max_tokens: int


class Routing(TypedDict):
    sort: Literal["throughput"]


class Body(TypedDict):
    model: str
    messages: list[Message]
    tools: list[ToolSchema]
    stream: bool
    reasoning_effort: NotRequired[Effort]
    stream_options: NotRequired[StreamOptions]
    reasoning: NotRequired[Reasoning]
    tool_choice: NotRequired[Literal["required"]]
    provider: NotRequired[Routing]


class DeltaFunction(TypedDict, total=False):
    name: str
    arguments: str


class DeltaCall(TypedDict, total=False):
    index: Required[int]
    id: str
    function: DeltaFunction


class Delta(TypedDict, total=False):
    content: str | None
    reasoning_content: str | None
    reasoning: str | None
    tool_calls: list[DeltaCall]


class Choice(TypedDict, total=False):
    delta: Delta
    finish_reason: str | None


class PromptDetails(TypedDict, total=False):
    cached_tokens: int


class UsageData(TypedDict, total=False):
    prompt_tokens: int
    completion_tokens: int
    prompt_cache_hit_tokens: int
    prompt_tokens_details: PromptDetails | None


class ApiError(TypedDict, total=False):
    message: str
    code: int | str


class Event(TypedDict, total=False):
    choices: list[Choice]
    usage: UsageData | None
    error: ApiError


@dataclass(frozen=True)
class Answer:
    content: str
    reasoning: str
    calls: tuple[Call, ...]
    finish: str | None
    tokens: Tokens | None
    seconds: float
    first: float | None


@dataclass(frozen=True)
class Failure:
    reason: str


@dataclass(frozen=True)
class Retry:
    reason: str
    wait: float | None


@dataclass
class Building:
    id: str | None = None
    name: str | None = None
    arguments: list[str] = field(default_factory=list)


@dataclass
class Collected:
    content: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    calls: list[Building] = field(default_factory=list)
    finish: str | None = None
    tokens: Tokens | None = None
    problem: str | None = None
    code: int | str | None = None
    first: float | None = None


@dataclass
class Usage:
    calls: int = 0
    replayed: int = 0
    redone: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    retried: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, answer: Answer) -> None:
        with self.lock:
            self.calls += 1
            self.seconds += answer.seconds
            if answer.tokens is not None:
                self.prompt_tokens += answer.tokens.prompt
                self.cached_tokens += answer.tokens.cached
                self.completion_tokens += answer.tokens.completion

    def replay(self) -> None:
        with self.lock:
            self.replayed += 1

    def redo(self) -> None:
        with self.lock:
            self.redone += 1

    def retry(self) -> None:
        with self.lock:
            self.retried += 1


def slot(calls: list[Building], index: int) -> Building:
    while len(calls) <= index:
        calls.append(Building())
    return calls[index]


def take_call(found: Collected, raw: DeltaCall) -> None:
    building = slot(found.calls, raw["index"])
    function = raw.get("function", {})
    name = function.get("name")
    arguments = function.get("arguments")
    call_id = raw.get("id")
    if call_id is not None:
        building.id = call_id
    if name is not None:
        building.name = name
    if arguments is not None:
        building.arguments.append(arguments)


def take_delta(found: Collected, delta: Delta) -> None:
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    content = delta.get("content")
    if reasoning is not None:
        found.reasoning.append(reasoning)
    if content is not None:
        found.content.append(content)
    for raw in delta.get("tool_calls") or []:
        take_call(found, raw)


def tokens_of(usage: UsageData) -> Tokens:
    details = usage.get("prompt_tokens_details") or {}
    cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0))
    return Tokens(usage.get("prompt_tokens", 0), cached, usage.get("completion_tokens", 0))


def take_event(found: Collected, event: Event) -> None:
    problem = event.get("error")
    usage = event.get("usage")
    if problem is not None:
        found.problem = problem.get("message", json.dumps(problem))
        found.code = problem.get("code")
    if usage is not None:
        found.tokens = tokens_of(usage)
    for choice in event.get("choices", []):
        finish = choice.get("finish_reason")
        if finish is not None:
            found.finish = finish
        take_delta(found, choice.get("delta", {}))


def answer_of(found: Collected, start: float, seconds: float) -> Answer | Failure:
    calls: list[Call] = []
    for building in found.calls:
        if building.id is None or building.name is None:
            return Failure("the model sent a tool call without an id or a name")
        else:
            calls.append(Call(building.id, building.name, "".join(building.arguments)))
    first: float | None = None
    if found.first is not None:
        first = found.first - start
    if found.problem is not None:
        return Failure(f"the model reported an error: {found.problem}")
    else:
        return Answer(
            content="".join(found.content),
            reasoning="".join(found.reasoning),
            calls=tuple(calls),
            finish=found.finish,
            tokens=found.tokens,
            seconds=seconds,
            first=first,
        )


def without_reasoning(message: Message) -> Message:
    if message["role"] == "assistant":
        kept: AssistantMessage = {"role": "assistant", "content": message["content"]}
        if "tool_calls" in message:
            kept["tool_calls"] = message["tool_calls"]
        return kept
    else:
        return message


def reasoning_dropped(messages: Sequence[Message]) -> list[Message]:
    found: list[Message] = []
    for message in messages:
        found.append(without_reasoning(message))
    return found


def request_body(messages: Sequence[Message], tools: Sequence[ToolSchema], settings: Settings, effort: Effort, required: bool) -> Body:
    body: Body
    match settings.provider:
        case "deepseek":
            body = {
                "model": settings.model,
                "messages": list(messages),
                "tools": list(tools),
                "stream": True,
                "reasoning_effort": effort,
                "stream_options": {"include_usage": True},
            }
        case "openrouter":
            reasoning: Reasoning = {"effort": effort}
            if settings.thinking > 0:
                reasoning = {"max_tokens": settings.thinking}
            body = {
                "model": settings.model,
                "messages": reasoning_dropped(messages),
                "tools": list(tools),
                "stream": True,
                "reasoning": reasoning,
                "provider": {"sort": "throughput"},
            }
    if required:
        body["tool_choice"] = "required"
    return body


def decoded_lines(response: http.client.HTTPResponse) -> Iterator[str]:
    for raw in response:
        yield raw.decode(errors="replace").rstrip("\r\n")


def waited(header: str | None, longest: float) -> float | None:
    if header is not None and header.strip().isdigit():
        return min(float(header.strip()), longest)
    else:
        return None


def said_in(call: Span, event: Event) -> None:
    for choice in event.get("choices", []):
        delta = choice.get("delta", {})
        chunk(call, "thinking", delta.get("reasoning_content") or delta.get("reasoning") or "")
        chunk(call, "text", delta.get("content") or "")
        for raw in delta.get("tool_calls") or []:
            chunk(call, "input", raw.get("function", {}).get("arguments") or "")


def spent_of(outcome: Answer | Failure, retries: list[str]) -> dict[str, JSON]:
    reasons: list[JSON] = []
    for reason in retries:
        reasons.append(reason)
    spent: dict[str, JSON] = {"retries": reasons}
    match outcome:
        case Failure(reason):
            spent["reason"] = cut(reason)
        case Answer(tokens=tokens, seconds=seconds, first=first):
            spent["turns"] = 1
            spent["api_seconds"] = seconds
            if first is not None:
                spent["first"] = first
            if tokens is not None:
                spent["prompt"] = tokens.prompt
                spent["cached"] = tokens.cached
                spent["completion"] = tokens.completion
    return spent


def prompt_size(messages: Sequence[Message]) -> int:
    total = 0
    for each in messages:
        total += len(each["content"])
        if each["role"] == "assistant":
            for call in each.get("tool_calls", []):
                total += len(call["function"]["arguments"])
    return total


class Client:
    usage: Usage
    settings: Settings
    subject: str
    cwd: Path
    tried: list[str]

    def __init__(self, usage: Usage, settings: Settings, subject: str, cwd: Path) -> None:
        self.usage = usage
        self.settings = settings
        self.subject = subject
        self.cwd = cwd
        self.tried = []

    def attempt(self, payload: bytes, call: Span) -> Answer | Failure | Retry:
        parts = urlsplit(self.settings.endpoint)
        key = PROVIDER_KEYS[self.settings.provider]
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if key != "":
            headers["Authorization"] = f"Bearer {key}"
        opened = http.client.HTTPConnection
        if parts.scheme == "https":
            opened = http.client.HTTPSConnection
        found = Collected()
        start = time.monotonic()
        try:
            connection = opened(parts.netloc, timeout=self.settings.call_timeout)
            try:
                refused = self.streamed(connection, parts.path, payload, headers, found, call)
            finally:
                connection.close()
        except (OSError, http.client.HTTPException) as error:
            return Retry(f"the connection failed: {type(error).__name__}: {error}", None)
        except json.JSONDecodeError as error:
            return Retry(f"a line of the reply was not JSON: {error}", None)
        else:
            return self.settled(found, refused, start)

    def streamed(
        self, connection: http.client.HTTPConnection, path: str, payload: bytes, headers: dict[str, str], found: Collected, call: Span
    ) -> Failure | Retry | None:
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        if response.status == 200:
            for line in decoded_lines(response):
                if line.startswith("data: ") and line != "data: [DONE]":
                    event: Event = json.loads(line.removeprefix("data: "))
                    if found.first is None:
                        found.first = time.monotonic()
                    take_event(found, event)
                    said_in(call, event)
            return None
        elif response.status in TRANSIENT:
            text = response.read().decode(errors="replace")[:2000]
            return Retry(f"HTTP {response.status}: {text}", waited(response.getheader("Retry-After"), max(self.settings.retry_waits, default=0.0)))
        else:
            text = response.read().decode(errors="replace")[:2000]
            return Failure(f"HTTP {response.status}: {text}")

    def settled(self, found: Collected, refused: Failure | Retry | None, start: float) -> Answer | Failure | Retry:
        if refused is not None:
            return refused
        elif found.finish is None and found.problem is None:
            return Retry("the reply ended before it said it was finished", None)
        elif found.problem is not None and found.code in TRANSIENT:
            return Retry(f"the model reported error {found.code} partway: {found.problem}", None)
        else:
            outcome = answer_of(found, start, time.monotonic() - start)
            if isinstance(outcome, Answer):
                self.usage.add(outcome)
            return outcome

    def ask(self, payload: bytes, call: Span) -> Answer | Failure:
        self.tried = []
        waits = self.settings.retry_waits
        outcome = self.attempt(payload, call)
        while isinstance(outcome, Retry) and len(self.tried) < len(waits):
            self.tried.append(outcome.reason)
            self.usage.retry()
            chunk(call, "note", f"sent again: {outcome.reason}")
            if outcome.wait is None:
                time.sleep(waits[len(self.tried) - 1])
            else:
                time.sleep(outcome.wait)
            outcome = self.attempt(payload, call)
        if isinstance(outcome, Retry):
            return Failure(f"{outcome.reason}, on the last of {len(self.tried) + 1} tries")
        else:
            return outcome

    def chat(self, messages: Sequence[Message], tools: Sequence[Tool], effort: Effort) -> Answer | Failure:
        return self.sent(messages, tools, effort, False)

    def answer(self, messages: Sequence[Message], tool: Tool, effort: Effort) -> Answer | Failure:
        return self.sent(messages, (tool,), effort, True)

    def sent(self, messages: Sequence[Message], tools: Sequence[Tool], effort: Effort, required: bool) -> Answer | Failure:
        payload = json.dumps(request_body(messages, schemas(tools), self.settings, effort, required)).encode()
        purpose = ""
        if len(tools) > 0:
            purpose = tools[-1].name
        size = prompt_size(messages)
        if size > self.settings.prompt_limit:
            reason = f"the prompt is {size} characters, over lain's limit of {self.settings.prompt_limit}, so it was not sent"
            noticed("model", project_of(str(self.cwd)), f"a {purpose} prompt was too long to send", f"{self.subject}: {reason}")
            return Failure(reason)
        given: dict[str, JSON] = {"api": self.settings.provider, "model": self.settings.model, "purpose": purpose, "subject": self.subject, "command": command_line()}
        with span("call", f"{self.settings.model} {purpose}", project_of(str(self.cwd)), here(), given) as call:
            outcome = self.ask(payload, call)
            call.ok = isinstance(outcome, Answer)
            call.attrs.update(spent_of(outcome, self.tried))
        if isinstance(outcome, Answer) and outcome.finish == "length":
            return Failure("the model's reply was cut off at its length limit")
        else:
            return outcome
