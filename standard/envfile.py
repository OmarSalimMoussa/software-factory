"""A workspace's or slot's env file, written from the product's template.

The file is the template's text, comments kept. The [env.set] keys and the factory's service keys are set at every
write. Every other key keeps a value set by hand; one that still holds the default the template had at the last write
takes the template's current value, so a changed default reaches every workspace and slot. The defaults of the last
write are kept beside the file (``<file>.defaults``); a file written before that record existed keeps every value.
Generated secrets are filled once, then kept. A key the template no longer has is dropped. On a server, a key under a
`# server: <why>` line must be set by hand.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import tempfile
from dataclasses import dataclass
from pathlib import Path

from standard.config import Config, render

SERVICE_BLOCK = "# Written by the factory for this workspace at every deploy; do not edit."
SERVER_MARKER = "# server:"
_ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_HEX = re.compile(r"^(.*?)generate-with-openssl-rand-hex-([0-9]+)$")
_FERNET = "generate-with-python-fernet"
_MAX_HEX_BYTES = 256


class EnvError(Exception):
    """The env file cannot be written as asked; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True, slots=True)
class _Line:
    text: str
    key: str  # "" for a comment or a blank line
    value: str
    server_why: str  # "" when no `# server:` line sits directly above


def placeholder_values(
    *, workspace: str, origin: str, port: int, commit: str, dir: Path, env_dir: Path, docker_socket: str
) -> dict[str, str]:
    """One value for each of ``config.PLACEHOLDERS``."""
    return {
        "workspace": workspace,
        "origin": origin,
        "port": str(port),
        "commit": commit,
        "dir": str(dir),
        "env_dir": str(env_dir),
        "docker_socket": docker_socket,
    }


def write(
    config: Config,
    *,
    template: Path,
    target: Path,
    placeholders: dict[str, str],
    services: dict[str, str],
    server: bool,
) -> list[str]:
    """Write ``target`` and return notes for the reader (keys added, generated or dropped; never a value)."""
    problems: list[str] = []
    lines = _template(template, problems)
    existing = _existing(target, problems)
    defaults = _existing(defaults_path(target), problems) if existing is not None else None
    keys = [line.key for line in lines if line.key]
    rendered = _rendered(config, placeholders, keys, template, problems)
    for key, value in {**rendered, **services}.items():
        if key in rendered and key in services:
            problems.append(
                f"{key} is an [env.set] key and also one the factory's services write; drop it from [env.set]"
            )
        if "\n" in value or "\r" in value:
            problems.append(f"{key}: a value may not hold a line break")
    if problems:
        raise EnvError(problems)

    notes = [f"created {target} from {template.name}"] if existing is None else []
    if existing is not None and defaults is None:
        notes.append(f"kept every value in {target}: no record of the template's defaults at its last write")
    held = existing or {}
    out: list[str] = []
    refusals: list[str] = []
    for line in lines:
        if not line.key:
            out.append(line.text)
            continue
        value, note = _value(line, rendered, services, held, defaults, existing is None)
        if note:
            notes.append(note)
        if server and line.server_why and line.key not in rendered and line.key not in services:
            if value == "":
                refusals.append(f"{line.key} is empty; a server workspace sets it by hand: {line.server_why}")
            elif value == line.value:
                refusals.append(
                    f"{line.key} still holds the template's value; a server workspace sets it by hand: "
                    f"{line.server_why}"
                )
        out.append(f"{line.key}={value}")
    extra = [key for key in services if key not in keys]
    if extra:
        out += ["", SERVICE_BLOCK] + [f"{key}={services[key]}" for key in extra]
    notes += [
        f"dropped {key}: the template no longer has it" for key in held if key not in keys and key not in services
    ]
    atomic_write(target, "\n".join(out) + "\n", 0o600)
    atomic_write(defaults_path(target), "".join(f"{line.key}={line.value}\n" for line in lines if line.key), 0o600)
    if refusals:
        raise EnvError([*refusals, f"set each in {target}, then deploy again"])
    return notes


def generate(marker: str) -> str | None:
    """The value a generate marker asks for, or None when ``marker`` is a plain value."""
    if marker == _FERNET:
        return base64.urlsafe_b64encode(os.urandom(32)).decode()
    match = _HEX.fullmatch(marker)
    if match is None:
        return None
    return match.group(1) + secrets.token_hex(int(match.group(2)))


def parse_values(text: str, where: str) -> tuple[dict[str, str], list[str]]:
    """The KEY=value pairs of an env file's text, and a problem for each line that is neither that nor a comment."""
    values: dict[str, str] = {}
    problems: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if not raw.strip() or raw.startswith("#"):
            continue
        match = _ASSIGN.fullmatch(raw)
        if match is None:
            problems.append(f"{where} line {number} is not KEY=value or a # comment")
        elif match.group(1) in values:
            problems.append(f"{where} line {number} sets {match.group(1)} a second time")
        else:
            values[match.group(1)] = match.group(2)
    return values, problems


def defaults_path(target: Path) -> Path:
    """Where the template's values at the last write of ``target`` are kept."""
    return target.with_name(f"{target.name}.defaults")


def _value(
    line: _Line,
    rendered: dict[str, str],
    services: dict[str, str],
    held: dict[str, str],
    defaults: dict[str, str] | None,
    fresh: bool,
) -> tuple[str, str]:
    if line.key in rendered:
        return rendered[line.key], ""
    if line.key in services:
        return services[line.key], ""
    current = held.get(line.key)
    if current is not None and defaults is not None and current == defaults.get(line.key) != line.value:
        made = generate(line.value)
        return (line.value if made is None else made), f"updated {line.key}: the template's default changed"
    if current is not None and current not in ("", line.value):
        return current, ""
    made = generate(line.value)
    if made is not None:
        return made, "" if fresh else f"generated {line.key}"
    if current is not None:
        return current, ""
    return line.value, "" if fresh else f"added {line.key} from the template"


def _rendered(
    config: Config, placeholders: dict[str, str], keys: list[str], template: Path, problems: list[str]
) -> dict[str, str]:
    rendered: dict[str, str] = {}
    for key, fmt in config.env.set:
        if key not in keys:
            problems.append(f"[env.set] {key} is not in {template.name}; add it to the template")
        try:
            rendered[key] = render(fmt, placeholders)
        except (KeyError, IndexError, ValueError) as exc:
            problems.append(f"[env.set] {key}: cannot fill {fmt!r} ({type(exc).__name__}: {exc})")
    return rendered


def _template(path: Path, problems: list[str]) -> list[_Line]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{path}: cannot read the template ({type(exc).__name__}: {exc})")
        return []
    lines: list[_Line] = []
    seen: set[str] = set()
    why = ""
    why_at = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        match = _ASSIGN.fullmatch(raw)
        if why and match is None:
            problems.append(f"{path.name} line {why_at}: a `{SERVER_MARKER}` line must sit directly above a KEY=value")
            why = ""
        if raw.startswith(SERVER_MARKER):
            why, why_at = raw[len(SERVER_MARKER) :].strip(), number
            if not why:
                problems.append(f"{path.name} line {number}: `{SERVER_MARKER}` needs a reason after it")
            lines.append(_Line(raw, "", "", ""))
            continue
        if match is None:
            if raw.strip() and not raw.startswith("#"):
                problems.append(f"{path.name} line {number} is not KEY=value or a # comment")
            lines.append(_Line(raw, "", "", ""))
            continue
        key, value = match.group(1), match.group(2)
        if key in seen:
            problems.append(f"{path.name} line {number} sets {key} a second time")
        seen.add(key)
        hex_marker = _HEX.fullmatch(value)
        if hex_marker is not None and not 1 <= int(hex_marker.group(2)) <= _MAX_HEX_BYTES:
            problems.append(f"{path.name} line {number}: {key} asks for 1 to {_MAX_HEX_BYTES} random bytes")
        lines.append(_Line(raw, key, value, why))
        why = ""
    if why:
        problems.append(f"{path.name} line {why_at}: a `{SERVER_MARKER}` line must sit directly above a KEY=value")
    return lines


def _existing(path: Path, problems: list[str]) -> dict[str, str] | None:
    if path.is_symlink():
        problems.append(f"{path} is a symbolic link; pass the real env file")
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{path}: cannot read the env file ({type(exc).__name__}: {exc})")
        return None
    values, found = parse_values(text, str(path))
    problems += found
    return values


def atomic_write(target: Path, text: str, mode: int) -> None:
    """Replace ``target`` whole: a reader sees the old file or the new one, never part of either."""
    target.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp creates the file 0600, so a secret is never readable by others, even before the chmod.
    handle, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
            file.flush()
            os.fsync(file.fileno())
        os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
