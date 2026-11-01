from __future__ import annotations

import ast
import os
import re
import stat
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast

from memory import covers
from page import Builder, Page, Unreadable, number, read, write
from store import locked
from tree import content, files_under, hashed, hidden

type Language = Literal["python", "kotlin", "java"]
type SymbolKind = Literal["class", "interface", "object", "fun", "val", "var", "typealias", "def", "enum", "record", "@interface", "method"]
type State = Literal["code", "block", "raw", "string"]
type Declared = Callable[[str, str], tuple[SymbolKind, str] | None]

MAP = "map"
FILES = "files"
LARGEST = 1_000_000
SIGNATURE = 160
SHAPE = 3
LANGUAGES: dict[str, Language] = {".py": "python", ".kt": "kotlin", ".kts": "kotlin", ".java": "java"}
MANIFESTS = (
    "build.gradle",
    "build.gradle.kts",
    "settings.gradle",
    "settings.gradle.kts",
    "pom.xml",
    "package.json",
    "pyproject.toml",
    "setup.py",
    "Cargo.toml",
    "go.mod",
)
GRADLE_SETTINGS = ("settings.gradle", "settings.gradle.kts")
GRADLE_BUILDS = ("build.gradle", "build.gradle.kts")
CONTAINERS = ("class", "interface", "object", "enum", "record", "@interface")
CALLABLE = ("fun", "def", "method")
ANNOTATIONS = r"(?:@[\w.:]+(?:\([^)]*\))?\s+)*"
GENERICS = r"(?:<(?:[^<>]|<[^<>]*>)*>\s*)?"
RECEIVER = r"(?:[\w.?<>, *]+\.)?"
KOTLIN = re.compile(
    rf"^\s*{ANNOTATIONS}(?P<mods>(?:(?:public|private|protected|internal|abstract|open|final|sealed|data|enum|annotation|inner|value|inline|"
    r"override|suspend|operator|infix|tailrec|external|const|lateinit|expect|actual|companion|fun(?=\s+interface))\s+)*)"
    r"(?P<kind>class|interface|object|fun|val|var|typealias)\b(?P<rest>.*)"
)
TYPE_NAME = re.compile(r"\s+(\w+)")
FUN_NAME = re.compile(rf"\s*{GENERICS}{RECEIVER}(\w+)\s*\(")
PROPERTY_NAME = re.compile(rf"\s*{GENERICS}{RECEIVER}(\w+)\s*(?::|=|\bby\b|$)")
PARAMETER = re.compile(r"\b(val|var)\s+(\w+)")
JAVA_TYPE = re.compile(
    rf"^\s*{ANNOTATIONS}(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed|strictfp)\s+)*(?P<kind>class|interface|enum|record|@interface)\s+(?P<name>\w+)"
)
JAVA_METHOD = re.compile(
    rf"^\s*{ANNOTATIONS}(?:(?:public|private|protected|static|final|abstract|synchronized|native|default|strictfp)\s+)*{GENERICS}"
    r"(?:(?P<type>[\w.<>\[\]?, ]+?)\s+)?(?P<name>\w+)\s*\("
)
STATEMENTS = frozenset(("return", "new", "throw", "if", "else", "for", "while", "switch", "case", "catch", "try", "do", "yield", "assert", "super", "this", "synchronized"))
TYPE = re.compile(r"\b[A-Z]\w*")
PACKAGE = re.compile(r"^\s*package\s+([\w.]+)")
IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)")
QUOTED = re.compile(r"""['"](:[\w.\-]+(?::[\w.\-]+)*)['"]""")
PROJECT = re.compile(r"""project\s*\(\s*(?:path\s*[:=]\s*)?['"](:[\w.\-:]+)['"]""")
LEADS = ("{", ")", "}", "]", ".", "?.", "?:", ":", "=", "->", "&&", "||", "+", "-", "*", "/", ",", "where ", "by ")
TRAILS = ("=", "(", "[", ",", ".", "->", "{", ":", "&&", "||", "+", "-", "*")


class SymbolData(TypedDict):
    name: str
    kind: SymbolKind
    line: int
    end: int
    signature: str
    digest: str


class ShapeData(TypedDict):
    package: str
    symbols: list[SymbolData]
    imports: list[str]
    names: list[str]


class StatData(TypedDict):
    size: int
    mtime: int
    digest: str
    lines: int


@dataclass(frozen=True)
class Source:
    path: str
    digest: str
    lines: int
    language: Language | None
    shape: ShapeData | None


@dataclass(frozen=True)
class Module:
    path: str
    manifests: tuple[str, ...]
    depends: tuple[str, ...]


@dataclass(frozen=True)
class Line:
    code: str
    depth: int
    low: int
    inside: bool


@dataclass(frozen=True)
class Container:
    name: str
    depth: int
    end: int


def language(path: str) -> Language | None:
    return LANGUAGES.get(PurePosixPath(path).suffix)


def symbol_kind(text: str) -> SymbolKind:
    match text:
        case "class" | "interface" | "object" | "fun" | "val" | "var" | "typealias" | "def" | "enum" | "record" | "@interface" | "method":
            return text
        case _:
            raise ValueError(f"not a kind of declaration: {text}")


def parent_of(path: str) -> str:
    head, slash, _ = path.rstrip("/").rpartition("/")
    if slash == "":
        return "."
    else:
        return f"{head}/"


def depth_of(path: str) -> int:
    if path == ".":
        return 0
    else:
        return path.rstrip("/").count("/") + 1


def base_of(directory: str) -> str:
    if directory == ".":
        return ""
    else:
        return directory


def signature_of(text: str) -> str:
    line = text.strip()
    if len(line) > SIGNATURE:
        return f"{line[:SIGNATURE]}..."
    else:
        return line


def scanned(text: str) -> list[Line]:
    found: list[Line] = []
    state: State = "code"
    nesting = 0
    depth = 0
    for raw in text.split("\n"):
        inside = state != "code"
        start = depth
        low = depth
        chars: list[str] = []
        index = 0
        while index < len(raw):
            char = raw[index]
            pair = raw[index : index + 2]
            step = 1
            if state == "block":
                if pair == "*/":
                    nesting -= 1
                    step = 2
                    if nesting == 0:
                        state = "code"
                elif pair == "/*":
                    nesting += 1
                    step = 2
                chars.append(" " * step)
            elif state == "raw":
                if raw.startswith('"""', index):
                    step = 3
                    while raw.startswith('"', index + step):
                        step += 1
                    state = "code"
                    chars.append('"' * step)
                else:
                    chars.append(" ")
            elif state == "string":
                if char == "\\":
                    step = 2
                    chars.append("  ")
                elif char == '"':
                    state = "code"
                    chars.append('"')
                else:
                    chars.append(" ")
            elif pair == "//":
                step = len(raw) - index
            elif pair == "/*":
                state = "block"
                nesting = 1
                step = 2
                chars.append("  ")
            elif raw.startswith('"""', index):
                state = "raw"
                step = 3
                chars.append('"""')
            elif char == '"':
                state = "string"
                chars.append('"')
            elif char == "'":
                close = index + 2
                if raw[index + 1 : index + 2] == "\\":
                    close = index + 3
                if raw[close : close + 1] == "'":
                    step = close - index + 1
                    chars.append(" " * step)
                else:
                    chars.append(char)
            elif char in "{([":
                depth += 1
                chars.append(char)
            elif char in "})]":
                depth -= 1
                low = min(low, depth)
                chars.append(char)
            else:
                chars.append(char)
            index += step
        if state == "string":
            state = "code"
        found.append(Line("".join(chars), start, low, inside))
    return found


def carries_on(code: str, before: str) -> bool:
    return code.startswith(LEADS) or before.rstrip().endswith(TRAILS)


def extent(lines: Sequence[Line], start: int) -> int:
    base = lines[start].depth
    end = start
    for index in range(start + 1, len(lines)):
        line = lines[index]
        code = line.code.strip()
        if line.inside or line.depth > base:
            if code != "" or line.inside:
                end = index
        elif line.depth < base or line.low < base:
            return end
        elif code == "":
            pass
        elif carries_on(code, lines[end].code):
            end = index
        else:
            return end
    return end


def group(found: re.Match[str] | None) -> str | None:
    if found is None:
        return None
    else:
        return found[1]


def kotlin_name(kind: SymbolKind, rest: str) -> str | None:
    match kind:
        case "fun":
            return group(FUN_NAME.match(rest))
        case "val" | "var":
            return group(PROPERTY_NAME.match(rest))
        case _:
            return group(TYPE_NAME.match(rest))


def kotlin_declared(code: str, container: str) -> tuple[SymbolKind, str] | None:
    found = KOTLIN.match(code)
    if found is None:
        return None
    else:
        kind = symbol_kind(found["kind"])
        name = kotlin_name(kind, found["rest"])
        if name is not None:
            return kind, name
        elif kind == "object" and "companion" in found["mods"]:
            return kind, "Companion"
        else:
            return None


def java_declared(code: str, container: str) -> tuple[SymbolKind, str] | None:
    typed = JAVA_TYPE.match(code)
    method = JAVA_METHOD.match(code)
    first = code.strip().partition(" ")[0].partition("(")[0]
    if typed is not None:
        return symbol_kind(typed["kind"]), typed["name"]
    elif method is None or first in STATEMENTS or method["name"] in STATEMENTS:
        return None
    elif method["type"] is None and method["name"] != container:
        return None
    else:
        return "method", method["name"]


class Braced:
    lines: list[Line]
    raw: list[str]
    declared: Declared
    package: str
    imports: list[str]
    used: set[str]
    symbols: list[SymbolData]
    stack: list[Container]

    def __init__(self, text: str, declared: Declared) -> None:
        self.lines = scanned(text)
        self.raw = text.split("\n")
        self.declared = declared
        self.package = ""
        self.imports = []
        self.used = set()
        self.symbols = []
        self.stack = []

    def shape(self) -> ShapeData:
        for index, line in enumerate(self.lines):
            if not line.inside:
                self.take(index, line)
        own: set[str] = set()
        for symbol in self.symbols:
            if "." not in symbol["name"]:
                own.add(symbol["name"])
        return {"package": self.package, "symbols": self.symbols, "imports": self.imports, "names": sorted(self.used - own)}

    def take(self, index: int, line: Line) -> None:
        packaged = PACKAGE.match(line.code)
        imported = IMPORT.match(line.code)
        if line.depth == 0 and packaged is not None:
            self.package = packaged[1]
        elif line.depth == 0 and imported is not None:
            self.imports.append(imported[1])
        else:
            self.used.update(TYPE.findall(line.code))
            while len(self.stack) > 0 and index > self.stack[-1].end:
                self.stack.pop()
            if line.depth == self.wanted():
                hit = self.declared(line.code, self.inner())
                if hit is not None:
                    self.add(index, line, hit[0], hit[1])

    def wanted(self) -> int:
        if len(self.stack) == 0:
            return 0
        else:
            return self.stack[-1].depth + 1

    def inner(self) -> str:
        if len(self.stack) == 0:
            return ""
        else:
            return self.stack[-1].name.rpartition(".")[2]

    def qualified(self, name: str) -> str:
        if len(self.stack) == 0:
            return name
        else:
            return f"{self.stack[-1].name}.{name}"

    def symbol(self, name: str, kind: SymbolKind, start: int, end: int) -> SymbolData:
        body = "\n".join(self.raw[start : end + 1])
        return {"name": name, "kind": kind, "line": start + 1, "end": end + 1, "signature": signature_of(self.raw[start]), "digest": hashed(body.encode())}

    def add(self, index: int, line: Line, kind: SymbolKind, name: str) -> None:
        end = extent(self.lines, index)
        qualified = self.qualified(name)
        self.symbols.append(self.symbol(qualified, kind, index, end))
        if kind in CONTAINERS:
            for parameter in PARAMETER.finditer(line.code.partition("(")[2]):
                self.symbols.append(self.symbol(f"{qualified}.{parameter[2]}", symbol_kind(parameter[1]), index, index))
            self.stack.append(Container(qualified, line.depth, end))


def python_symbols(text: str, lines: Sequence[str], body: Sequence[ast.stmt], outer: str) -> list[SymbolData]:
    found: list[SymbolData] = []
    for node in body:
        if isinstance(node, ast.ClassDef):
            found.append(python_symbol(text, lines, node, f"{outer}{node.name}", "class"))
            found.extend(python_symbols(text, lines, node.body, f"{outer}{node.name}."))
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(python_symbol(text, lines, node, f"{outer}{node.name}", "def"))
    return found


def python_symbol(text: str, lines: Sequence[str], node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, name: str, kind: SymbolKind) -> SymbolData:
    segment = ast.get_source_segment(text, node) or ""
    end = node.end_lineno or node.lineno
    return {"name": name, "kind": kind, "line": node.lineno, "end": end, "signature": signature_of(lines[node.lineno - 1]), "digest": hashed(segment.encode())}


def python_imports(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.append(f"{'.' * node.level}{node.module or ''}:{alias.name}")
    return found


def python_shape(text: str) -> ShapeData:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return {"package": "", "symbols": [], "imports": [], "names": []}
    else:
        return {"package": "", "symbols": python_symbols(text, text.split("\n"), tree.body, ""), "imports": python_imports(tree), "names": []}


def shape(kind: Language, text: str) -> ShapeData:
    match kind:
        case "python":
            return python_shape(text)
        case "kotlin":
            return Braced(text, kotlin_declared).shape()
        case "java":
            return Braced(text, java_declared).shape()


def function_bodies(path: str, text: str) -> dict[str, str]:
    kind = language(path)
    found: dict[str, str] = {}
    if kind is not None:
        for symbol in shape(kind, text)["symbols"]:
            if symbol["kind"] in CALLABLE:
                found.setdefault(symbol["name"].rpartition(".")[2], symbol["digest"])
    return found


def listed(root: Path) -> list[str]:
    if (root / ".git").exists():
        done = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True)
        found: dict[str, None] = {}
        for raw in done.stdout.decode(errors="replace").split("\0"):
            if raw != "" and not hidden(raw):
                found[raw] = None
        return list(found)
    else:
        return files_under(root, ".")


def testing(path: str) -> bool:
    for part in PurePosixPath(path).parts[:-1]:
        if part == "test" or part.endswith("Test"):
            return True
    return False


def dotted(path: str) -> str:
    return path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def shared_prefix(first: str, second: str) -> int:
    count = 0
    for one, other in zip(first.split("/"), second.split("/")):
        if one != other:
            return count
        else:
            count += 1
    return count


class Skeleton:
    root: Path
    sources: dict[str, Source]
    modules: dict[str, Module]
    children: dict[str, list[str]]
    uses: dict[str, tuple[str, ...]] | None
    users: dict[str, tuple[str, ...]] | None

    def __init__(self, root: Path, sources: dict[str, Source]) -> None:
        self.root = root
        self.sources = sources
        self.children = {".": []}
        for path in sorted(sources):
            self.place(path, parent_of(path))
        self.modules = modules_of(root, sources)
        self.uses = None
        self.users = None

    def place(self, child: str, parent: str) -> None:
        fresh = parent not in self.children
        self.children.setdefault(parent, []).append(child)
        if fresh:
            self.place(parent, parent_of(parent))

    def directories(self) -> list[str]:
        return sorted(self.children, key=lambda path: (-depth_of(path), path))

    def under(self, directory: str) -> list[str]:
        found: list[str] = []
        for path in self.sources:
            if covers(directory, path):
                found.append(path)
        return found

    def module_of(self, path: str) -> Module | None:
        best: Module | None = None
        for module in self.modules.values():
            if covers(module.path, path) and (best is None or len(module.path) > len(best.path)):
                best = module
        return best

    def depended_on_by(self, module: Module) -> list[str]:
        found: list[str] = []
        for other in self.modules.values():
            if module.path in other.depends:
                found.append(other.path)
        return sorted(found)

    def links(self) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
        if self.uses is None or self.users is None:
            self.uses, self.users = resolved(self.sources)
        return self.uses, self.users


def included_in(root: Path, directory: str, name: str) -> dict[str, str]:
    found: dict[str, str] = {}
    base = base_of(directory)
    for line in (root / f"{base}{name}").read_text(errors="replace").splitlines():
        if line.strip().startswith("include"):
            for project in QUOTED.findall(line):
                found[project] = f"{base}{project.strip(':').replace(':', '/')}/"
    return found


def depends_of(root: Path, directory: str, name: str, included: dict[str, str], manifests: dict[str, list[str]]) -> list[str]:
    found: list[str] = []
    for project in PROJECT.findall((root / f"{base_of(directory)}{name}").read_text(errors="replace")):
        wanted = included.get(project, f"{project.strip(':').replace(':', '/')}/")
        if wanted in manifests and wanted != directory and wanted not in found:
            found.append(wanted)
    return found


def modules_of(root: Path, sources: dict[str, Source]) -> dict[str, Module]:
    manifests: dict[str, list[str]] = {}
    for path in sources:
        name = PurePosixPath(path).name
        if name in MANIFESTS:
            manifests.setdefault(parent_of(path), []).append(name)
    included: dict[str, str] = {}
    for directory, names in manifests.items():
        for name in names:
            if name in GRADLE_SETTINGS:
                included.update(included_in(root, directory, name))
    found: dict[str, Module] = {}
    for directory, names in manifests.items():
        depends: list[str] = []
        for name in names:
            if name in GRADLE_BUILDS:
                for wanted in depends_of(root, directory, name, included, manifests):
                    if wanted not in depends:
                        depends.append(wanted)
        found[directory] = Module(directory, tuple(sorted(names)), tuple(sorted(depends)))
    return found


def relative_target(sources: dict[str, Source], importer: str, wanted: str) -> str | None:
    level = len(wanted) - len(wanted.lstrip("."))
    base = PurePosixPath(importer).parent
    for _ in range(level - 1):
        base = base.parent
    rest = wanted.lstrip(".").replace(".", "/")
    candidates = [f"{base.as_posix()}/__init__.py"]
    if rest != "":
        stem = (base / rest).as_posix()
        candidates = [f"{stem}.py", f"{stem}/__init__.py"]
    for candidate in candidates:
        clean = candidate.removeprefix("./")
        if clean in sources:
            return clean
    return None


def absolute_target(named: dict[str, list[str]], importer: str, wanted: str) -> str | None:
    best: str | None = None
    score = -1
    for path in named.get(wanted.rpartition(".")[2], []):
        name = dotted(path)
        if (name == wanted or name.endswith(f".{wanted}")) and shared_prefix(path, importer) > score:
            best = path
            score = shared_prefix(path, importer)
    return best


def python_target(sources: dict[str, Source], named: dict[str, list[str]], importer: str, wanted: str) -> str | None:
    if wanted.startswith("."):
        return relative_target(sources, importer, wanted)
    else:
        return absolute_target(named, importer, wanted)


def python_tries(wanted: str) -> list[str]:
    module, colon, name = wanted.partition(":")
    if colon == "":
        return [module]
    elif module == "" or module.endswith("."):
        return [f"{module}{name}", module]
    else:
        return [f"{module}.{name}", module]


def python_targets(sources: dict[str, Source], named: dict[str, list[str]], importer: str, wanted: str) -> list[str]:
    for each in python_tries(wanted):
        if each != "":
            found = python_target(sources, named, importer, each)
            if found is not None:
                return [found]
    return []


def jvm_targets(top: dict[tuple[str, str], list[str]], names: Sequence[str], wanted: str) -> list[str]:
    if wanted.endswith(".*"):
        found: list[str] = []
        for name in names:
            found.extend(top.get((wanted.removesuffix(".*"), name), []))
        return found
    else:
        parts = wanted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            declared = top.get((".".join(parts[:cut]), parts[cut]))
            if declared is not None:
                return declared
        return []


def jvm_uses(top: dict[tuple[str, str], list[str]], path: str, shaped: ShapeData) -> set[str]:
    found: set[str] = set()
    for wanted in [*shaped["imports"], f"{shaped['package']}.*"]:
        for target in jvm_targets(top, shaped["names"], wanted):
            if testing(path) or not testing(target):
                found.add(target)
    return found


def resolved(sources: dict[str, Source]) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    named: dict[str, list[str]] = {}
    top: dict[tuple[str, str], list[str]] = {}
    for path, source in sources.items():
        if source.language == "python":
            named.setdefault(dotted(path).rpartition(".")[2], []).append(path)
        elif source.shape is not None:
            for symbol in source.shape["symbols"]:
                if "." not in symbol["name"]:
                    top.setdefault((source.shape["package"], symbol["name"]), []).append(path)
    uses: dict[str, tuple[str, ...]] = {}
    users: dict[str, set[str]] = {}
    for path, source in sources.items():
        if source.shape is not None:
            found: set[str] = set()
            if source.language == "python":
                for wanted in source.shape["imports"]:
                    found.update(python_targets(sources, named, path, wanted))
            else:
                found = jvm_uses(top, path, source.shape)
            found.discard(path)
            uses[path] = tuple(sorted(found))
            for target in found:
                users.setdefault(target, set()).add(path)
    used: dict[str, tuple[str, ...]] = {}
    for path, importers in users.items():
        used[path] = tuple(sorted(importers))
    return uses, used


def cache_path(home: Path, path: str) -> Path:
    return home / FILES / f"{path}.md"


def cache_page(path: str, seen: StatData, key: str | None, shaped: ShapeData | None) -> Page:
    built = Builder(f"what lain's map knows of {path}")
    built.field("path", path)
    built.field("size", str(seen["size"]))
    built.field("modified", str(seen["mtime"]))
    built.field("digest", seen["digest"])
    built.field("lines", str(seen["lines"]))
    if key is not None and shaped is not None:
        built.field("shape", key)
        built.field("package", shaped["package"])
        for each in shaped["imports"]:
            built.field("import", each)
        for each in shaped["names"]:
            built.field("uses", each)
        for symbol in shaped["symbols"]:
            built.field("symbol", f"{symbol['kind']} {symbol['line']} {symbol['end']} {symbol['digest']} {symbol['name']}")
            built.field("signature", " ".join(symbol["signature"].split()))
    return built.page()


def shape_of(page: Page) -> ShapeData:
    symbols: list[SymbolData] = []
    for line, signature in zip(page.every("symbol"), page.every("signature")):
        kind, start, end, seen, name = line.split(" ", 4)
        symbols.append({"name": name, "kind": symbol_kind(kind), "line": number(start), "end": number(end), "signature": signature, "digest": seen})
    return {"package": page.one("package") or "", "symbols": symbols, "imports": page.every("import"), "names": page.every("uses")}


def cache_in(home: Path) -> tuple[dict[str, StatData], dict[str, ShapeData]]:
    stats: dict[str, StatData] = {}
    shapes: dict[str, ShapeData] = {}
    if (home / FILES).is_dir():
        for path in (home / FILES).rglob("*.md"):
            try:
                page = read(path)
                stats[page.need("path")] = {
                    "size": number(page.need("size")),
                    "mtime": number(page.need("modified")),
                    "digest": page.need("digest"),
                    "lines": number(page.need("lines")),
                }
                key = page.one("shape")
                if key is not None and key.startswith(f"{SHAPE}:"):
                    shapes[key] = shape_of(page)
            except (Unreadable, ValueError):
                path.unlink()
    return stats, shapes


class Scan:
    root: Path
    known: dict[str, StatData]
    parsed: dict[str, ShapeData]
    stats: dict[str, StatData]
    keys: dict[str, str]
    sources: dict[str, Source]

    def __init__(self, root: Path, known: dict[str, StatData], parsed: dict[str, ShapeData]) -> None:
        self.root = root
        self.known = known
        self.parsed = parsed
        self.stats = {}
        self.keys = {}
        self.sources = {}

    def take(self, path: str) -> None:
        try:
            info = os.stat(self.root / path)
            if stat.S_ISREG(info.st_mode):
                self.taken(path, info)
        except OSError:
            pass

    def taken(self, path: str, info: os.stat_result) -> None:
        kind = language(path)
        old = self.known.get(path)
        if old is not None and old["size"] == info.st_size and old["mtime"] == info.st_mtime_ns:
            self.kept(path, kind, old, None, info.st_size)
        else:
            read = content(self.root / path, kind is not None and info.st_size <= LARGEST)
            seen: StatData = {"size": info.st_size, "mtime": info.st_mtime_ns, "digest": read.digest, "lines": read.lines}
            self.kept(path, kind, seen, read.data, info.st_size)

    def kept(self, path: str, kind: Language | None, seen: StatData, data: bytes | None, size: int) -> None:
        self.stats[path] = seen
        self.sources[path] = Source(path, seen["digest"], seen["lines"], kind, self.shaped(path, kind, seen["digest"], data, size))

    def shaped(self, path: str, kind: Language | None, digest: str, data: bytes | None, size: int) -> ShapeData | None:
        if kind is None or size > LARGEST:
            return None
        else:
            key = f"{SHAPE}:{kind}:{digest}"
            if key not in self.parsed:
                self.parse(key, kind, data or (self.root / path).read_bytes())
            if key in self.parsed:
                self.keys[path] = key
                return self.parsed[key]
            else:
                return None

    def parse(self, key: str, kind: Language, data: bytes) -> None:
        if b"\x00" not in data:
            self.parsed[key] = shape(kind, data.decode(errors="replace"))

    def save(self, home: Path) -> None:
        for path, seen in self.stats.items():
            if self.known.get(path) != seen or not cache_path(home, path).is_file():
                key = self.keys.get(path)
                shaped: ShapeData | None = None
                if key is not None:
                    shaped = self.parsed[key]
                cache_path(home, path).parent.mkdir(parents=True, exist_ok=True)
                write(cache_path(home, path), cache_page(path, seen, key, shaped))
        for path in self.known:
            if path not in self.stats:
                cache_path(home, path).unlink(missing_ok=True)


def skeleton(home: Path, root: Path) -> Skeleton:
    folder = home / MAP
    with locked(folder):
        stats, parsed = cache_in(folder)
        scan = Scan(root, stats, parsed)
        for path in listed(root):
            scan.take(path)
        scan.save(folder)
    return Skeleton(root, scan.sources)
