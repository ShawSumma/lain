from __future__ import annotations

import ast
import io
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
BODIES = ("body", "orelse", "finalbody")
LEAVING = (ast.Return, ast.Continue, ast.Break, ast.Raise)
LOOSE = ("Any", "object")
GENERIC = ("list", "dict", "set", "frozenset", "tuple", "type", "Callable", "Sequence", "Mapping", "Iterable", "Iterator", "Counter", "deque")
REGEX_AFTER = "(,=:[!&|?{};"


@dataclass(frozen=True)
class Found:
    path: Path
    line: int
    what: str


def comments(path: Path, text: str) -> list[Found]:
    found: list[Found] = []
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT and not (token.start[0] == 1 and token.string.startswith("#!")):
            found.append(Found(path, token.start[0], "comment"))
    return found


def docstring(path: Path, node: ast.AST) -> list[Found]:
    if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and len(node.body) > 0:
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            return [Found(path, first.lineno, "docstring")]
        else:
            return []
    else:
        return []


def leaves(statements: list[ast.stmt]) -> bool:
    return len(statements) > 0 and isinstance(statements[-1], LEAVING)


def fallen(path: Path, statements: list[ast.stmt]) -> list[Found]:
    found: list[Found] = []
    for index, statement in enumerate(statements[:-1]):
        if isinstance(statement, ast.If) and len(statement.orelse) == 0 and leaves(statement.body):
            found.append(Found(path, statement.lineno, "if that leaves without an else, with more after it"))
        elif isinstance(statement, ast.Try) and len(statement.orelse) == 0 and len(statement.finalbody) == 0:
            for handler in statement.handlers:
                if leaves(handler.body):
                    found.append(Found(path, handler.lineno, "except that leaves, with the rest after the try instead of in its else"))
    return found


def statement_lists(node: ast.AST) -> list[list[ast.stmt]]:
    found: list[list[ast.stmt]] = []
    for name in BODIES:
        value = getattr(node, name, None)
        if isinstance(value, list) and len(value) > 0 and isinstance(value[0], ast.stmt):
            found.append(value)
    return found


def annotations_of(node: ast.AST) -> list[ast.expr]:
    found: list[ast.expr] = []
    if isinstance(node, ast.arg | ast.AnnAssign) and node.annotation is not None:
        found.append(node.annotation)
    elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
        found.append(node.returns)
    elif isinstance(node, ast.TypeAlias):
        found.append(node.value)
    return found


def subscripted(annotation: ast.expr) -> set[int]:
    found: set[int] = set()
    for part in ast.walk(annotation):
        if isinstance(part, ast.Subscript):
            found.add(id(part.value))
    return found


def loose(path: Path, node: ast.AST) -> list[Found]:
    found: list[Found] = []
    for annotation in annotations_of(node):
        given = subscripted(annotation)
        for part in ast.walk(annotation):
            if isinstance(part, ast.Name) and part.id in LOOSE:
                found.append(Found(path, part.lineno, f"{part.id} in a type"))
            elif isinstance(part, ast.Name) and part.id in GENERIC and id(part) not in given:
                found.append(Found(path, part.lineno, f"{part.id} with no type arguments"))
    return found


def parameters(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.arg]:
    found: list[ast.arg] = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    if node.args.vararg is not None:
        found.append(node.args.vararg)
    if node.args.kwarg is not None:
        found.append(node.args.kwarg)
    return found


def unannotated(path: Path, node: ast.AST, methods: set[int]) -> list[Found]:
    found: list[Found] = []
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        if node.returns is None:
            found.append(Found(path, node.lineno, f"{node.name} has no return type"))
        for index, parameter in enumerate(parameters(node)):
            receiver = index == 0 and id(node) in methods
            if parameter.annotation is None and not receiver:
                found.append(Found(path, parameter.lineno, f"{node.name}'s {parameter.arg} has no type"))
    return found


def methods_in(tree: ast.Module) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                    found.add(id(member))
    return found


def any_renamed(names: list[ast.alias]) -> bool:
    for name in names:
        if name.asname is not None:
            return True
    return False


def checked(path: Path) -> list[Found]:
    text = path.read_text()
    found = comments(path, text)
    tree = ast.parse(text)
    methods = methods_in(tree)
    for node in ast.walk(tree):
        found.extend(docstring(path, node))
        found.extend(loose(path, node))
        found.extend(unannotated(path, node, methods))
        if isinstance(node, ast.Import | ast.ImportFrom) and any_renamed(node.names):
            found.append(Found(path, node.lineno, "import under another name"))
        if isinstance(node, COMPREHENSIONS):
            found.append(Found(path, node.lineno, "comprehension"))
        elif isinstance(node, ast.IfExp):
            found.append(Found(path, node.lineno, "ternary"))
        for statements in statement_lists(node):
            found.extend(fallen(path, statements))
    return found


class Scanner:
    path: Path
    text: str
    index: int
    line: int
    found: list[Found]

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.text = text
        self.index = 0
        self.line = 1
        self.found = []

    def at(self, offset: int) -> str:
        return self.text[self.index + offset : self.index + offset + 1]

    def step(self) -> None:
        if self.at(0) == "\n":
            self.line += 1
        self.index += 1

    def note(self, what: str) -> None:
        self.found.append(Found(self.path, self.line, what))

    def code(self, closing: bool) -> None:
        depth = 0
        last = ""
        while self.index < len(self.text):
            char = self.at(0)
            ahead = self.at(1)
            if char == "}" and closing and depth == 0:
                self.step()
                break
            elif char in "'\"":
                self.quoted(char)
                last = char
            elif char == "`":
                self.template()
                last = char
            elif char == "/" and ahead in ("/", "*"):
                self.note("comment")
                self.comment(ahead)
            elif char == "/" and last != "" and last in REGEX_AFTER:
                self.regex()
                last = char
            else:
                last = self.plain(char, ahead, last)
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1

    def plain(self, char: str, ahead: str, last: str) -> str:
        if char == "?" and ahead not in (".", "?") and last != "?":
            self.note("ternary")
        self.step()
        if char.isspace():
            return last
        else:
            return char

    def quoted(self, quote: str) -> None:
        self.step()
        while self.index < len(self.text):
            char = self.at(0)
            self.step()
            if char == "\\":
                self.step()
            elif char == quote:
                break

    def template(self) -> None:
        self.step()
        while self.index < len(self.text):
            char = self.at(0)
            if char == "\\":
                self.step()
                self.step()
            elif char == "`":
                self.step()
                break
            elif char == "$" and self.at(1) == "{":
                self.step()
                self.step()
                self.code(True)
            else:
                self.step()

    def comment(self, kind: str) -> None:
        end = "*/"
        if kind == "/":
            end = "\n"
        while self.index < len(self.text) and not self.text.startswith(end, self.index):
            self.step()
        if end == "*/":
            self.step()
            self.step()

    def regex(self) -> None:
        self.step()
        within = False
        while self.index < len(self.text) and self.at(0) != "\n":
            char = self.at(0)
            self.step()
            if char == "\\":
                self.step()
            elif char == "[":
                within = True
            elif char == "]":
                within = False
            elif char == "/" and not within:
                break


def script_checked(path: Path) -> list[Found]:
    scanner = Scanner(path, path.read_text())
    scanner.code(False)
    return scanner.found


def file_checked(path: Path) -> list[Found]:
    if path.suffix == ".js":
        return script_checked(path)
    else:
        return checked(path)


def sources(given: list[str]) -> list[Path]:
    found: list[Path] = []
    if len(given) == 0:
        found.extend(sorted((PROJECT / "src").glob("*.py")))
        found.append(PROJECT / "export.py")
        found.extend(sorted((PROJECT / "web").glob("*.js")))
    else:
        for each in given:
            found.append(Path(each).resolve())
    return found


def main(argv: list[str]) -> int:
    found: list[Found] = []
    for path in sources(argv):
        found.extend(file_checked(path))
    for each in sorted(found, key=lambda each: (str(each.path), each.line)):
        print(f"{each.path.relative_to(PROJECT)}:{each.line}: {each.what}")
    if len(found) > 0:
        return 1
    else:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
