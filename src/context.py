from __future__ import annotations

from collections.abc import Sequence

from history import Brief, Note, Reply, Request, Result, Step
from model import AssistantMessage, Message, ToolCallData


def assistant(reply: Reply) -> AssistantMessage:
    said: AssistantMessage = {"role": "assistant", "content": reply.content, "reasoning_content": reply.thinking}
    calls: list[ToolCallData] = []
    for call in reply.calls:
        calls.append({"id": call.id, "type": "function", "function": {"name": call.tool, "arguments": call.arguments}})
    if len(calls) > 0:
        said["tool_calls"] = calls
    return said


def render(system: str, lineage: Sequence[Step]) -> list[Message]:
    messages: list[Message] = [{"role": "system", "content": system}]
    for step in lineage:
        match step.body:
            case Reply() as reply:
                messages.append(assistant(reply))
            case Result(call=call, text=text):
                messages.append({"role": "tool", "tool_call_id": call, "content": text})
            case Brief(text=text) | Note(text=text) | Request(text=text):
                messages.append({"role": "user", "content": text})
            case _:
                pass
    return messages
