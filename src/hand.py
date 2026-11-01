from __future__ import annotations

from history import History, Request, Step
from memory import Memory, Store
from places import USER, Places
from prompts import message
from store import locked


def by_hand(found: Places, said: str) -> tuple[str, Step]:
    with locked(found.agent(USER)):
        step = History(found.agent(USER)).append(Request(said))
    return f"{USER}/{step.number}", step


def forgotten(found: Places, store: Store, target: Memory, reason: str) -> Memory:
    text = reason.strip()
    if text == "":
        text = message("forgotten")
    said = f"forget {target.id}: {text}"
    source, step = by_hand(found, said)
    return store.retract(target, text, "user", source, step.time, (said,))


def corrected(found: Places, store: Store, target: Memory, text: str | None) -> Memory:
    said = f"pin {target.id}"
    kept = target.text
    if text is not None:
        kept = text.strip()
        said = f"correct {target.id}: {kept}"
    source, step = by_hand(found, said)
    return store.correct(target, kept, store.current_files(target), target.conditions, "user", source, step.time, (said,))
