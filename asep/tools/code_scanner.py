from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
_TABLE_RE = re.compile(r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+(\w+)", re.IGNORECASE)


@dataclass
class Route:
    method: str
    path: str
    handler: str
    lineno: int


@dataclass
class ModuleInfo:
    path: str
    module: str
    imports: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)
    loc: int = 0

    @property
    def symbols(self) -> list[str]:
        return self.classes + self.functions


class CodebaseIndex:
    """Static index of an existing codebase.

    Impact analysis is answered from here rather than from recollection: which
    module defines a symbol is a fact on the AST.
    """

    def __init__(self, root: Path, modules: list[ModuleInfo]):
        self.root = root
        self.modules = modules
        self.by_path = {m.path: m for m in modules}
        self.defines: dict[str, list[str]] = {}
        for module in modules:
            for symbol in module.symbols:
                self.defines.setdefault(symbol, []).append(module.path)

    @property
    def routes(self) -> list[tuple[str, Route]]:
        return [(m.path, r) for m in self.modules for r in m.routes]

    @property
    def tables(self) -> list[str]:
        return sorted({t for m in self.modules for t in m.tables})

    def module_of(self, symbol: str) -> list[str]:
        return self.defines.get(symbol, [])

    def importers_of(self, module_path: str) -> list[str]:
        """Modules whose imports resolve to the given file."""
        stem = Path(module_path).stem
        out = []
        for module in self.modules:
            if module.path == module_path:
                continue
            if any(_names_module(imported, stem) for imported in module.imports):
                out.append(module.path)
        return sorted(set(out))

    def references_to(self, symbol: str) -> list[str]:
        out = []
        for module in self.modules:
            if symbol in module.imports or any(
                imported.split(".")[-1] == symbol for imported in module.imports
            ):
                out.append(module.path)
        return sorted(set(out))

    def summary(self) -> dict:
        return {
            "modules": len(self.modules),
            "loc": sum(m.loc for m in self.modules),
            "routes": len(self.routes),
            "tables": self.tables,
        }


def _names_module(imported: str, stem: str) -> bool:
    """Does this recorded import refer to the module called `stem`?

    `import app.db` records `app.db`; `from .db import Database` records
    `db.Database`, where the last component is a symbol rather than the module.
    Both readings are checked, or every `from x import y` would be missed.

    Name-level only: two modules sharing a basename are not told apart.
    """
    parts = imported.split(".")
    return parts[-1] == stem or (len(parts) > 1 and parts[-2] == stem)


def _decorator_route(node: ast.AST) -> tuple[str, str] | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    method = node.func.attr.lower()
    if method not in HTTP_METHODS:
        return None
    if not node.args or not isinstance(node.args[0], ast.Constant):
        return None
    value = node.args[0].value
    if not isinstance(value, str):
        return None
    return method.upper(), value


def scan_module(path: Path, root: Path) -> ModuleInfo:
    source = path.read_text(encoding="utf-8", errors="replace")
    info = ModuleInfo(
        path=path.relative_to(root).as_posix(),
        module=path.stem,
        loc=source.count("\n") + 1,
    )
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return info

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            info.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            info.imports.extend(
                f"{base}.{alias.name}" if base else alias.name for alias in node.names
            )
        elif isinstance(node, ast.ClassDef):
            info.classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info.functions.append(node.name)
            for decorator in node.decorator_list:
                route = _decorator_route(decorator)
                if route:
                    info.routes.append(
                        Route(
                            method=route[0],
                            path=route[1],
                            handler=node.name,
                            lineno=node.lineno,
                        )
                    )

    info.tables = _TABLE_RE.findall(source)
    return info


def scan(root: Path, include_tests: bool = True) -> CodebaseIndex:
    root = Path(root).resolve()
    modules: list[ModuleInfo] = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"__pycache__", ".venv", "venv", ".git", "node_modules"}:
            continue
        if not include_tests and path.name.startswith("test_"):
            continue
        modules.append(scan_module(path, root))

    for path in sorted(root.rglob("*.sql")):
        source = path.read_text(encoding="utf-8", errors="replace")
        modules.append(
            ModuleInfo(
                path=path.relative_to(root).as_posix(),
                module=path.stem,
                tables=_TABLE_RE.findall(source),
                loc=source.count("\n") + 1,
            )
        )
    return CodebaseIndex(root, modules)
