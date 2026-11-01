from __future__ import annotations

import os
import sys

from build import Broken, build, current


def entry() -> int:
    code = current()
    if code is None:
        found = build()
        if isinstance(found, Broken):
            print(f"lain has never been built, and could not be: {found.reason}", file=sys.stderr)
            return 1
        else:
            code = found
    cli = str(code / "src" / "cli.py")
    os.execv(sys.executable, [sys.executable, cli, *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(entry())
