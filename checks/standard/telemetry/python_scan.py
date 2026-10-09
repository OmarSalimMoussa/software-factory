"""Python source to facts, through `ast` and `tokenize`, in one tree walk.

Catch sites are every `except` handler and every `with suppress(...)`. First-party imports resolve to tracked files
through the package roots and first-party names the values give; relative imports resolve against the importing file.
"""

from __future__ import annotations

import ast
import dataclasses
import io
import posixpath
import tokenize
from collections.abc import Callable, Sequence
from typing import Any

from checks.standard.telemetry.facts import (
    MARKER_RE,
    CallRef,
    CatchSite,
    FileFacts,
    FunctionFacts,
    ImportRef,
    MarkerComment,
)

_SUPPRESS = ("suppress", "contextlib.suppress")


class Resolver:
    def __init__(self, roots: Sequence[str], first_party: Sequence[str], tracked: set[str]):
        self.roots = tuple(roots)
        self.first_party = frozenset(first_party)
        self.tracked = tracked

    def _file(self, base: str, parts: Sequence[str]) -> str | None:
        stem = posixpath.normpath(posixpath.join(base, *parts))
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if candidate in self.tracked:
                return candidate
        return None

    def module(self, path: str, name: str, level: int) -> str | None:
        parts = [p for p in name.split(".") if p]
        if level:
            base = posixpath.dirname(path)
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            return self._file(base or ".", parts)
        if not parts or parts[0] not in self.first_party:
            return None
        for root in self.roots:
            found = self._file(root, parts)
            if found:
                return found
        return None


def dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def call_ref(call: ast.Call) -> CallRef | None:
    func = call.func
    if isinstance(func, ast.Name):
        name, receiver = func.id, ""
    elif isinstance(func, ast.Attribute):
        name, receiver = func.attr, dotted(func.value) or "?"
    else:
        return None
    first: str | None = None
    if call.args:
        arg = call.args[0]
        literal = isinstance(arg, ast.Constant) and isinstance(arg.value, str | int)
        first = str(arg.value) if literal and isinstance(arg, ast.Constant) else dotted(arg)
    stderr = any(kw.arg == "file" and dotted(kw.value) == "sys.stderr" for kw in call.keywords)
    return CallRef(name, receiver, first, call.lineno, stderr)


@dataclasses.dataclass
class _Body:
    calls: list[CallRef] = dataclasses.field(default_factory=list)
    raises: bool = False


@dataclasses.dataclass(frozen=True)
class _Frame:
    """Where the walk is: the innermost function's body, the open handlers
    inside that function, the enclosing function node, and whether the
    direct parent is a class body."""

    func: _Body | None = None
    handlers: tuple[_Body, ...] = ()
    scope: ast.AST | None = None
    in_class: bool = False


class _Scanner:
    def __init__(self, path: str, text: str, resolver: Resolver):
        self.path, self.text, self.resolver = path, text, resolver
        self.lines = text.splitlines()
        self.sites: list[CatchSite] = []
        self.functions: list[FunctionFacts] = []
        self.imports: list[ImportRef] = []
        self.calls: dict[tuple[str, str, str | None], CallRef] = {}
        self.warns: list[int] = []

    def _scope_text(self, frame: _Frame) -> str:
        node = frame.scope
        if node is None:
            return self.text
        start, end = getattr(node, "lineno", 1), getattr(node, "end_lineno", None)
        return "\n".join(self.lines[start - 1 : end])

    def children(self, node: ast.AST, frame: _Frame) -> None:
        for field in node._fields:
            value = getattr(node, field, None)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item, frame)
            elif isinstance(value, ast.AST) and not isinstance(value, ast.expr_context):
                self.visit(value, frame)

    def visit(self, node: ast.AST, frame: _Frame) -> None:
        handler = _HANDLERS.get(type(node))
        if handler is None:
            self.children(node, frame)
        else:
            handler(self, node, frame)

    def _call(self, node: ast.Call, frame: _Frame) -> None:
        ref = call_ref(node)
        if ref is not None:
            self.calls.setdefault((ref.receiver, ref.name, ref.first_arg), ref)
            if frame.func is not None:
                frame.func.calls.append(ref)
            for body in frame.handlers:
                body.calls.append(ref)
            if ref.dotted == "warnings.warn":
                self.warns.append(node.lineno)
        self.children(node, frame)

    def _raise(self, node: ast.Raise, frame: _Frame) -> None:
        for body in frame.handlers:
            body.raises = True
        self.children(node, frame)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, frame: _Frame) -> None:
        body = _Body()
        inner = _Frame(func=body, scope=node)
        for stmt in node.body:
            self.visit(stmt, inner)
        method = frame.in_class
        self.functions.append(FunctionFacts(self.path, node.name, method, tuple(body.calls)))

    def _lambda(self, node: ast.Lambda, frame: _Frame) -> None:
        self.visit(node.body, _Frame(scope=frame.scope))

    def _class(self, node: ast.ClassDef, frame: _Frame) -> None:
        inner = _Frame(scope=frame.scope, in_class=True)
        for stmt in node.body:
            self.visit(stmt, inner)

    def _except(self, node: ast.ExceptHandler, frame: _Frame) -> None:
        body = _Body()
        inner = dataclasses.replace(frame, handlers=(*frame.handlers, body), in_class=False)
        for stmt in node.body:
            self.visit(stmt, inner)
        header_end = (node.type.end_lineno or node.lineno) if node.type else node.lineno
        self.sites.append(
            CatchSite(
                path=self.path,
                line=node.lineno,
                kind="except",
                detail=f"except {ast.unparse(node.type)}" if node.type else "except",
                body_line=header_end + 1,
                calls=tuple(body.calls),
                raises=body.raises,
                scope_text=self._scope_text(frame),
            )
        )

    def _with(self, node: ast.With | ast.AsyncWith, frame: _Frame) -> None:
        for item in node.items:
            expr = item.context_expr
            if isinstance(expr, ast.Call) and dotted(expr.func) in _SUPPRESS:
                site = CatchSite(
                    path=self.path,
                    line=node.lineno,
                    kind="suppress",
                    detail=ast.unparse(expr),
                    body_line=node.lineno + 1,
                    scope_text=self._scope_text(frame),
                )
                self.sites.append(site)
        self.children(node, frame)

    def _import(self, node: ast.Import, frame: _Frame) -> None:
        for alias in node.names:
            target = self.resolver.module(self.path, alias.name, 0)
            local = alias.asname or alias.name
            self.imports.append(ImportRef(self.path, local, target, None, node.lineno, specifier=alias.name))

    def _import_from(self, node: ast.ImportFrom, frame: _Frame) -> None:
        module = node.module or ""
        base = self.resolver.module(self.path, module, node.level)
        for alias in node.names:
            local = alias.asname or alias.name
            sub = self.resolver.module(self.path, f"{module}.{alias.name}", node.level)
            target, name = (sub, None) if sub is not None else (base, alias.name)
            self.imports.append(ImportRef(self.path, local, target, name, node.lineno, specifier=module))


# Each handler takes the node type its key names; the table erases that to Any.
_HANDLERS: dict[type[ast.AST], Callable[[_Scanner, Any, _Frame], None]] = {
    ast.Call: _Scanner._call,
    ast.Raise: _Scanner._raise,
    ast.FunctionDef: _Scanner._function,
    ast.AsyncFunctionDef: _Scanner._function,
    ast.Lambda: _Scanner._lambda,
    ast.ClassDef: _Scanner._class,
    ast.ExceptHandler: _Scanner._except,
    ast.With: _Scanner._with,
    ast.AsyncWith: _Scanner._with,
    ast.Import: _Scanner._import,
    ast.ImportFrom: _Scanner._import_from,
}


def _markers(path: str, text: str) -> tuple[MarkerComment, ...]:
    if "telemetry:" not in text:
        return ()
    found: list[MarkerComment] = []
    for tok in tokenize.generate_tokens(io.StringIO(text).readline):
        if tok.type == tokenize.COMMENT:
            body = tok.string.lstrip("#").strip()
            if MARKER_RE.match(body):
                found.append(MarkerComment(path, tok.start[0], body))
    return tuple(found)


def _main_guard(tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            parts = [node.test.left, *node.test.comparators]
            if any(isinstance(p, ast.Name) and p.id == "__name__" for p in parts):
                return True
    return False


def scan(path: str, text: str, resolver: Resolver) -> FileFacts:
    """Facts for one Python file; a syntax error propagates."""
    tree = ast.parse(text, filename=path)
    scanner = _Scanner(path, text, resolver)
    scanner.children(tree, _Frame())
    return FileFacts(
        path=path,
        sites=tuple(sorted(scanner.sites, key=lambda s: s.line)),
        functions=tuple(scanner.functions),
        imports=tuple(scanner.imports),
        markers=_markers(path, text),
        calls=tuple(scanner.calls.values()),
        warnings_warn=tuple(scanner.warns),
        main_guard=_main_guard(tree),
    )
