"""Runs ``node ts_scan.mjs`` over the TS/JS paths and rebuilds its facts.

All paths go in one ``node`` call. A missing node or typescript, a parse error, a non-zero exit or JSON of the wrong
shape raises ScanError: there is no regex fallback.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

from checks.standard.telemetry.facts import (
    SITE_KINDS,
    CallRef,
    CatchSite,
    FileFacts,
    FunctionFacts,
    ImportRef,
    MarkerComment,
    SiteKind,
)

SCANNER = Path(__file__).resolve().parent / "ts_scan.mjs"
_MISSING_TS = 3


class ScanError(RuntimeError):
    """A scanner could not produce facts."""


def _call(raw: dict[str, Any]) -> CallRef:
    return CallRef(
        name=str(raw["name"]),
        receiver=str(raw["receiver"]),
        first_arg=None if raw["first_arg"] is None else str(raw["first_arg"]),
        line=int(raw["line"]),
    )


def _site(raw: dict[str, Any]) -> CatchSite:
    kind = str(raw["kind"])
    if kind not in SITE_KINDS:
        raise ScanError(f"ts_scan emitted an unknown site kind {kind!r}")
    return CatchSite(
        path=str(raw["path"]),
        line=int(raw["line"]),
        kind=cast(SiteKind, kind),
        detail=str(raw["detail"]),
        body_line=int(raw["body_line"]),
        calls=tuple(_call(c) for c in raw["calls"]),
        raises=bool(raw["raises"]),
        handler=None if raw["handler"] is None else _call(raw["handler"]),
        scope_text=str(raw["scope_text"]),
    )


def _file(raw: dict[str, Any]) -> FileFacts:
    path = str(raw["path"])
    return FileFacts(
        path=path,
        sites=tuple(_site(s) for s in raw["sites"]),
        functions=tuple(
            FunctionFacts(path, str(f["name"]), bool(f["method"]), tuple(map(_call, f["calls"])))
            for f in raw["functions"]
        ),
        imports=tuple(
            ImportRef(
                path=path,
                local=str(i["local"]),
                target=i["target"],
                name=i["name"],
                line=int(i["line"]),
                type_only=bool(i["type_only"]),
                specifier=str(i["specifier"]),
            )
            for i in raw["imports"]
        ),
        markers=tuple(MarkerComment(path, int(m["line"]), str(m["text"])) for m in raw["markers"]),
        calls=tuple(_call(c) for c in raw["calls"]),
    )


def scan(root: Path, paths: Sequence[str], tracked: Sequence[str], typescript_from: str) -> list[FileFacts]:
    """Facts for every path in ``paths``, in order; ``typescript_from`` is the repo folder whose node_modules holds
    typescript."""
    if not paths:
        return []
    if not typescript_from:
        raise ScanError(
            f"{len(paths)} TS/JS file(s) are registered (first {paths[0]}), and typescript_from in "
            "check-values/error-telemetry.toml names no folder to load typescript from: set it to a folder whose "
            "node_modules holds typescript, or exclude those files with their grounds"
        )
    request = json.dumps(
        {"root": str(root), "toolchain": str(root / typescript_from), "paths": list(paths), "tracked": list(tracked)}
    )
    try:
        proc = subprocess.run(["node", str(SCANNER)], input=request, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise ScanError("node is not on PATH: install Node.js to scan TS/JS files") from exc
    if proc.returncode == _MISSING_TS:
        raise ScanError(proc.stderr.strip())
    if proc.returncode != 0:
        raise ScanError(f"ts_scan failed (exit {proc.returncode}): {proc.stderr.strip()}")
    try:
        files = json.loads(proc.stdout)["files"]
        return [_file(files[path]) for path in paths]
    except (KeyError, TypeError, ValueError) as exc:
        raise ScanError(f"ts_scan emitted facts of the wrong shape: {exc!r}") from exc
