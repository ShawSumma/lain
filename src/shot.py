from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None

WAIT = """
const selector = arguments[0];
const done = arguments[arguments.length - 1];
const look = () => {
  if (document.querySelector(selector) !== null) {
    done(true);
  } else {
    requestAnimationFrame(look);
  }
};
look();
"""

CLICK = """
document.querySelector(arguments[0]).dispatchEvent(new MouseEvent("click", { bubbles: true }));
"""


@dataclass(frozen=True)
class Failed:
    reason: str


class Marionette:
    connection: socket.socket
    reader: IO[bytes]
    count: int

    def __init__(self, port: int) -> None:
        self.connection = socket.create_connection(("127.0.0.1", port))
        self.reader = self.connection.makefile("rb")
        self.count = 0
        self.receive()

    def receive(self) -> Json:
        length = b""
        while not length.endswith(b":"):
            chunk = self.reader.read(1)
            if chunk == b"":
                raise ConnectionError("Firefox closed the Marionette connection")
            else:
                length += chunk
        data: Json = json.loads(self.reader.read(int(length[:-1])))
        return data

    def send(self, command: str, parameters: dict[str, Json]) -> Json | Failed:
        self.count += 1
        body = json.dumps([0, self.count, command, parameters]).encode()
        self.connection.sendall(f"{len(body)}:".encode() + body)
        reply = self.receive()
        match reply:
            case [1, _, None, result]:
                return result
            case [1, _, error, _]:
                return Failed(f"{command}: {json.dumps(error)}")
            case _:
                return Failed(f"{command}: unexpected reply {json.dumps(reply)}")


def listening(lines: IO[bytes]) -> int | None:
    for line in lines:
        text = line.decode(errors="replace")
        if "Listening on port" in text:
            return int(text.split()[-1])
    return None


def shot(marionette: Marionette, url: str, selector: str, clicks: list[tuple[str, str]]) -> bytes | Failed:
    steps: list[tuple[str, dict[str, Json]]] = [
        ("WebDriver:NewSession", {}),
        ("WebDriver:Navigate", {"url": url}),
        ("WebDriver:ExecuteAsyncScript", {"script": WAIT, "args": [selector]}),
    ]
    for clicked, then in clicks:
        steps.append(("WebDriver:ExecuteScript", {"script": CLICK, "args": [clicked]}))
        steps.append(("WebDriver:ExecuteAsyncScript", {"script": WAIT, "args": [then]}))
    for command, parameters in steps:
        done = marionette.send(command, parameters)
        if isinstance(done, Failed):
            return done
    taken = marionette.send("WebDriver:TakeScreenshot", {})
    match taken:
        case Failed():
            return taken
        case {"value": str(value)}:
            return base64.b64decode(value)
        case _:
            return Failed(f"WebDriver:TakeScreenshot: unexpected result {json.dumps(taken)}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="shot.py",
        description="Open a page in headless Firefox, wait until an element shows up, and save a screenshot, "
        "so a page that fills itself in after loading can be checked.",
    )
    parser.add_argument("url")
    parser.add_argument("selector", help="a CSS selector that matches once the page shows what to check")
    parser.add_argument("output", help="the PNG file to write")
    parser.add_argument("--profile", required=True, help="a Firefox profile directory, so a running Firefox is left alone")
    parser.add_argument(
        "--click",
        nargs=2,
        action="append",
        default=[],
        metavar=("CLICKED", "THEN"),
        help="click the first element CLICKED matches, then wait until THEN matches; give it more than once for several clicks in turn",
    )
    parsed = parser.parse_args(argv)
    url: str = parsed.url
    selector: str = parsed.selector
    output: str = parsed.output
    profile: str = parsed.profile
    given: list[list[str]] = parsed.click
    clicks: list[tuple[str, str]] = []
    for clicked, then in given:
        clicks.append((clicked, then))
    read, write = os.pipe()
    firefox = subprocess.Popen(["firefox", "--headless", "--no-remote", "--marionette", "--profile", profile], stdout=write, stderr=write)
    os.close(write)
    with open(read, "rb") as lines:
        port = listening(lines)
        if port is None:
            firefox.wait()
            print("Firefox exited before Marionette listened", file=sys.stderr)
            return 1
        else:
            threading.Thread(target=lines.read, daemon=True).start()
            marionette = Marionette(port)
            found = shot(marionette, url, selector, clicks)
            marionette.send("Marionette:Quit", {})
            firefox.wait()
    if isinstance(found, Failed):
        print(found.reason, file=sys.stderr)
        return 1
    else:
        Path(output).write_bytes(found)
        print(f"wrote {output}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
