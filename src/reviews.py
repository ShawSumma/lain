from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from extract import cut
from history import Brief, History, Result
from ingest import empty_state, read_state, tail, write_state
from model import Failure
from places import REVIEWS, Places
from redact import redacted
from store import JSON, locked

REPO = "repos/{owner}/{repo}"
PAGE = 100
TIMEOUT = 60.0
LONGEST = 8000
HUNK = 12
LISTED = 50
STATES = {"APPROVED": "approving it", "CHANGES_REQUESTED": "asking for changes", "COMMENTED": "commenting", "DISMISSED": "since dismissed"}

type Item = dict[str, JSON]


@dataclass(frozen=True)
class Said:
    ident: str
    time: float
    text: str


def gh(root: Path, path: str) -> JSON | Failure:
    argv = ["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json", path]
    env = {**os.environ, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
    try:
        done = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        return Failure("gh is not installed; reading pull requests needs the GitHub CLI, signed in with gh auth login")
    except subprocess.TimeoutExpired:
        return Failure(f"gh api {path} took longer than {TIMEOUT:.0f}s")
    else:
        if done.returncode != 0:
            return Failure(f"gh api {path} failed: {done.stderr.strip()}")
        else:
            return answered_json(path, done.stdout)


def answered_json(path: str, text: str) -> JSON | Failure:
    try:
        found: JSON = json.loads(text)
    except json.JSONDecodeError:
        return Failure(f"gh api {path} did not return JSON")
    else:
        return found


def pages(root: Path, path: str, query: str) -> list[Item] | Failure:
    found: list[Item] = []
    page = 1
    while True:
        got = gh(root, f"{path}?{query}per_page={PAGE}&page={page}")
        if isinstance(got, Failure):
            return got
        elif not isinstance(got, list):
            return Failure(f"gh api {path} did not return a list")
        else:
            for item in got:
                if isinstance(item, dict):
                    found.append(item)
            if len(got) < PAGE:
                return found
            else:
                page += 1


def field(data: Item, key: str) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value
    else:
        return ""


def number_of(data: Item, key: str) -> int | None:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    else:
        return None


def author(data: Item) -> tuple[str, bool]:
    user = data.get("user")
    if isinstance(user, dict):
        return field(user, "login"), field(user, "type") == "Bot"
    else:
        return "", False


def stamp(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def day(text: str) -> str:
    return text[:10]


def hunk(text: str) -> str:
    lines = text.splitlines()[-HUNK:]
    return "\n".join(["```diff", *lines, "```"])


def described(pr: Item) -> list[Said]:
    login, bot = author(pr)
    body = field(pr, "body").strip()
    number = number_of(pr, "number")
    if body == "" or bot:
        return []
    else:
        return [Said(f"pr{number}", stamp(field(pr, "created_at")), f"description by {login}:\n{body}")]


def reviewed(reviews: list[Item]) -> list[Said]:
    found: list[Said] = []
    for review in reviews:
        login, bot = author(review)
        state = field(review, "state")
        body = field(review, "body").strip()
        when = field(review, "submitted_at")
        if not bot and when != "" and state in STATES and (body != "" or state in ("APPROVED", "CHANGES_REQUESTED")):
            text = f"review by {login}, {STATES[state]}"
            if body != "":
                text = f"{text}:\n{body}"
            found.append(Said(f"review{number_of(review, 'id')}", stamp(when), text))
    return found


def on_code(comments: list[Item]) -> list[Said]:
    logins: dict[int, str] = {}
    for comment in comments:
        ident = number_of(comment, "id")
        if ident is not None:
            logins[ident] = author(comment)[0]
    found: list[Said] = []
    for comment in comments:
        login, bot = author(comment)
        body = field(comment, "body").strip()
        if not bot and body != "":
            line = number_of(comment, "line") or number_of(comment, "original_line")
            where = f"comment by {login} on {field(comment, 'path')}"
            if line is not None:
                where = f"{where} line {line}"
            reply = number_of(comment, "in_reply_to_id")
            if reply is not None and reply in logins:
                where = f"{where}, replying to {logins[reply]}"
            text = f"{where}:\n{hunk(field(comment, 'diff_hunk'))}\n{body}"
            found.append(Said(f"comment{number_of(comment, 'id')}", stamp(field(comment, "created_at")), text))
    return found


def conversation(comments: list[Item]) -> list[Said]:
    found: list[Said] = []
    for comment in comments:
        login, bot = author(comment)
        body = field(comment, "body").strip()
        if not bot and body != "":
            found.append(Said(f"note{number_of(comment, 'id')}", stamp(field(comment, "created_at")), f"comment by {login}:\n{body}"))
    return found


def heading(pr: Item, files: list[Item]) -> str:
    login, _ = author(pr)
    ended = f"closed without merging on {day(field(pr, 'closed_at'))}"
    if field(pr, "merged_at") != "":
        ended = f"merged on {day(field(pr, 'merged_at'))}"
    names: list[str] = []
    for each in files:
        names.append(field(each, "filename"))
    shown = ", ".join(names[:LISTED])
    if len(names) > LISTED:
        shown = f"{shown} and {len(names) - LISTED} more"
    lines = [
        f"pull request #{number_of(pr, 'number')}, opened on {day(field(pr, 'created_at'))} by {login}, {ended}",
        f"title: {field(pr, 'title')}",
        f"files changed: {shown}",
    ]
    return "\n".join(lines)


def read_one(root: Path, history: History, pr: Item, number: int) -> Failure | None:
    base = f"{REPO}/pulls/{number}"
    files = pages(root, f"{base}/files", "")
    reviews = pages(root, f"{base}/reviews", "")
    code = pages(root, f"{base}/comments", "")
    talk = pages(root, f"{REPO}/issues/{number}/comments", "")
    if isinstance(files, Failure):
        return files
    elif isinstance(reviews, Failure):
        return reviews
    elif isinstance(code, Failure):
        return code
    elif isinstance(talk, Failure):
        return talk
    else:
        said = described(pr) + reviewed(reviews) + on_code(code) + conversation(talk)
        said.sort(key=lambda each: each.time)
        opened = stamp(field(pr, "created_at"))
        history.append(Brief(redacted(heading(pr, files)), f"pr {number}", "", "github"), opened)
        for each in said:
            history.append(Result(each.ident, "comment", redacted(cut(each.text, LONGEST)), True, (), (), 0.0), max(each.time, opened))
        return None


def read_reviews(found: Places, batch: int | None) -> int | Failure:
    home = found.agent(REVIEWS)
    root = found.repo.root
    with locked(home):
        history = History(home)
        state = read_state(home)
        if state is None:
            state = empty_state(root)
        listed = pages(root, f"{REPO}/pulls", "state=closed&sort=created&direction=asc&")
        if isinstance(listed, Failure):
            return listed
        else:
            fresh: list[tuple[int, Item]] = []
            for pr in listed:
                number = number_of(pr, "number")
                if number is not None and str(number) not in state["steps"] and (batch is None or len(fresh) < batch):
                    fresh.append((number, pr))
            history.move(tail(state))
            for number, pr in fresh:
                failed = read_one(root, history, pr, number)
                if failed is not None:
                    return failed
                else:
                    state["steps"][str(number)] = history.head
                    write_state(home, state)
            return len(fresh)
