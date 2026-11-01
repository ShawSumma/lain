from __future__ import annotations

import ast
import builtins
import importlib
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from prompts import PROMPTS, sections

HERE = Path(__file__).resolve().parent


def bound_by(node: ast.AST) -> list[str]:
    match node:
        case ast.Name(id=name, ctx=ast.Store() | ast.Del()):
            return [name]
        case ast.arg(arg=name) | ast.FunctionDef(name=name) | ast.AsyncFunctionDef(name=name) | ast.ClassDef(name=name):
            return [name]
        case ast.alias(name=name, asname=None):
            return [name.partition(".")[0]]
        case ast.alias(asname=str(name)):
            return [name]
        case ast.ExceptHandler(name=str(name)) | ast.MatchAs(name=str(name)) | ast.MatchStar(name=str(name)):
            return [name]
        case ast.MatchMapping(rest=str(name)):
            return [name]
        case ast.TypeVar(name=name) | ast.ParamSpec(name=name) | ast.TypeVarTuple(name=name):
            return [name]
        case ast.Global(names=names) | ast.Nonlocal(names=names):
            return names
        case _:
            return []


def missing_text(node: ast.AST) -> str | None:
    match node:
        case ast.Call(func=ast.Name(id="message"), args=[ast.Constant(value=str(key)), *_]) if key not in sections("messages"):
            return f"the message {key} is not in prompts/messages.md"
        case ast.Call(func=ast.Name(id="tool_text"), args=[ast.Constant(value=str(key)), *_]) if key not in sections("tools"):
            return f"the tool text {key} is not in prompts/tools.md"
        case ast.Call(func=ast.Name(id="prompt"), args=[ast.Constant(value=str(name)), *_]) if not (PROMPTS / f"{name}.md").is_file():
            return f"the prompt prompts/{name}.md does not exist"
        case _:
            return None


@dataclass
class Used:
    keys: set[str] = field(default_factory=set)
    prefixes: set[str] = field(default_factory=set)
    unknown: list[str] = field(default_factory=list)

    def take(self, node: ast.AST, file: str) -> None:
        match node:
            case ast.Call(func=ast.Name(id="message"), args=[ast.Constant(value=str(key)), *_]):
                self.keys.add(key)
            case ast.Call(func=ast.Name(id="message"), args=[ast.JoinedStr(values=[ast.Constant(value=str(prefix)), *_]), *_]):
                self.prefixes.add(prefix)
            case ast.Call(func=ast.Name(id="message"), lineno=line):
                self.unknown.append(f"{file}:{line}")
            case _:
                pass

    def has(self, key: str) -> bool:
        if key in self.keys:
            return True
        else:
            for prefix in self.prefixes:
                if key.startswith(prefix):
                    return True
            return False


def unbound(module: ModuleType, used: Used) -> list[str]:
    if module.__file__ is None:
        return []
    else:
        path = Path(module.__file__)
        tree = ast.parse(path.read_text())
        known = set(vars(module)) | set(vars(builtins))
        for node in ast.walk(tree):
            known.update(bound_by(node))
        found: list[str] = []
        for node in ast.walk(tree):
            used.take(node, path.name)
            problem = missing_text(node)
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in known:
                found.append(f"{module.__name__}: {node.id} is used but never defined ({path.name}:{node.lineno})")
            elif problem is not None:
                found.append(f"{module.__name__}: {problem} ({path.name}:{node.lineno})")
        return found


def unused(used: Used) -> list[str]:
    found: list[str] = []
    if len(used.unknown) > 0:
        found.append(f"prompts/messages.md: unused messages are not listed, since some keys are only known when lain runs ({', '.join(used.unknown)})")
    else:
        for key in sections("messages"):
            if not used.has(key):
                found.append(f"prompts/messages.md: the message {key} is never used")
    return found


def main(argv: list[str]) -> int:
    names = list(argv)
    if len(argv) == 0:
        for path in sorted(HERE.glob("*.py")):
            names.append(path.stem)
    used = Used()
    problems: list[str] = []
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception as error:
            where = traceback.extract_tb(error.__traceback__)[-1]
            problems.append(f"{name}: {type(error).__name__}: {error} ({Path(where.filename).name}:{where.lineno})")
        else:
            problems.extend(unbound(module, used))
    if len(argv) == 0:
        problems.extend(unused(used))
    for line in problems:
        print(line)
    return min(len(problems), 1)


if __name__ == "__main__":
    sys.path.insert(0, str(HERE))
    sys.exit(main(sys.argv[1:]))
