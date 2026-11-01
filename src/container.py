from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from config import Settings
from model import Failure
from places import LAIN, Repo
from tree import hashed

DOCKERFILE = "Dockerfile"
WORK = "/work"


@dataclass(frozen=True)
class Container:
    image: str
    network: bool

    def argv(self, root: Path, command: Sequence[str]) -> list[str]:
        found = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1"]
        if not self.network:
            found.extend(["--network", "none"])
        found.extend(["--volume", f"{root}:{WORK}", "--workdir", WORK, self.image, *command])
        return found


def dockerfile(repo: Repo, settings: Settings) -> Path:
    path = repo.root / LAIN / DOCKERFILE
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(settings.dockerfile)
    return path


def prepared(repo: Repo, settings: Settings) -> Container | Failure:
    text = dockerfile(repo, settings).read_text()
    tag = f"lain-{repo.name.lower()}:{hashed(text.encode())[:12]}"
    try:
        found = subprocess.run(["docker", "image", "inspect", tag], capture_output=True)
    except FileNotFoundError:
        return Failure("docker is not installed")
    if found.returncode == 0:
        return Container(tag, settings.container_network)
    else:
        built = subprocess.run(["docker", "build", "--quiet", "--tag", tag, "-"], input=text.encode(), capture_output=True)
        if built.returncode == 0:
            return Container(tag, settings.container_network)
        else:
            return Failure(f"docker could not build {repo.root / LAIN / DOCKERFILE}: {built.stderr.decode(errors='replace').strip()[-2000:]}")
