from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

LAIN = ".lain"
BUILDS = "builds"
CODE = Path(__file__).resolve().parent.parent
SESSIONS = "sessions"
GIT = "git"
REVIEWS = "reviews"
CHECKS = "checks"
USER = "user"
NOTES = "notes"
RUNS = "runs"


def install_of(code: Path) -> Path:
    if code.parent.name == BUILDS and code.parents[1].name == "install" and code.parents[2].name == LAIN:
        return code.parents[1]
    else:
        return code / LAIN / "install"


INSTALL = install_of(CODE)
CHECKOUT = INSTALL.parent.parent
LAUNCHER = CHECKOUT / "src" / "lain.py"


@dataclass(frozen=True)
class Repo:
    root: Path
    main: Path

    @property
    def name(self) -> str:
        return self.main.name


@dataclass(frozen=True)
class Places:
    repo: Repo
    home: Path

    @property
    def memory(self) -> Path:
        return self.home / "memory"

    def agent(self, name: str) -> Path:
        return self.home / name


def worktree_main(candidate: Path, marker: Path) -> Path:
    text = marker.read_text().strip()
    if text.startswith("gitdir:"):
        gitdir = Path(text.removeprefix("gitdir:").strip())
        if not gitdir.is_absolute():
            gitdir = (candidate / gitdir).resolve()
        if gitdir.parent.name == "worktrees":
            return gitdir.parent.parent.parent
        else:
            return candidate
    else:
        return candidate


def repo_of(directory: Path) -> Repo:
    for candidate in (directory, *directory.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return Repo(candidate, candidate)
        elif marker.is_file():
            return Repo(candidate, worktree_main(candidate, marker))
    return Repo(directory, directory)


def in_checkout(directory: Path) -> bool:
    return (repo_of(directory).root / ".git").exists()


def ignored(home: Path) -> Path:
    if not (home / ".gitignore").is_file():
        home.mkdir(parents=True, exist_ok=True)
        (home / ".gitignore").write_text("*\n")
    return home


def places(directory: Path) -> Places:
    repo = repo_of(directory)
    return Places(repo, ignored(repo.main / LAIN))
