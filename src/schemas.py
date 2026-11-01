from __future__ import annotations

import json
import sys

from rpc import OFFERED
from spec import BEFORE, COMMAND, COMMIT_NOTES, CONTRADICTED, DESCRIBE, EXTRACT, LIST_DIR, MATCHES, READ_FILE, RECHECK, SEARCH, parameters


def main(argv: list[str]) -> int:
    for tool in (LIST_DIR, READ_FILE, SEARCH, BEFORE, COMMAND, COMMIT_NOTES, EXTRACT, MATCHES, RECHECK, CONTRADICTED, DESCRIBE, *OFFERED):
        print(f"{tool.name}: {len(json.dumps(parameters(tool)))} characters of schema")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
