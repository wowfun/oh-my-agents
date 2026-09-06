from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from hagency_cli.workspace.errors import fail


def read_toml(path: Path) -> dict:
    if not path.exists():
        fail(f"missing config: {path}")
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        fail(f"cannot read config {path}: {exc}")


def write_toml(path: Path, data: dict) -> None:
    content = render_toml(data)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def toml_value(value: Any) -> str:
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, list):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    fail(f"unsupported TOML value type: {type(value).__name__}")


def toml_key_part(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return toml_value(value)


def toml_table(parts: list[str]) -> str:
    return "[" + ".".join(toml_key_part(part) for part in parts) + "]"


def append_scalar_lines(lines: list[str], mapping: dict, keys: list[str]) -> None:
    seen = set()
    for key in keys:
        if key in mapping and mapping[key] is not None:
            lines.append(f"{key} = {toml_value(mapping[key])}")
            seen.add(key)
    for key, value in mapping.items():
        if key in seen or isinstance(value, dict | list) or value is None:
            continue
        lines.append(f"{key} = {toml_value(value)}")


def render_toml(data: dict) -> str:
    lines: list[str] = []
    top_level = {
        key: value
        for key, value in data.items()
        if key not in {"defaults", "source", "skill"}
        and not isinstance(value, dict | list)
        and value is not None
    }
    if top_level:
        append_scalar_lines(lines, top_level, ["name", "description"])

    defaults = data.get("defaults")
    if defaults:
        if lines:
            lines.append("")
        lines.append("[defaults]")
        append_scalar_lines(
            lines,
            defaults,
            [
                "checkout_dir",
                "checkout_dir_windows",
                "depth",
                "remote_name",
                "remote_ref",
            ],
        )

    for name, raw_source in data.get("source", {}).items():
        if lines:
            lines.append("")
        lines.append(toml_table(["source", name]))
        append_scalar_lines(lines, raw_source, ["path"])
        remote = raw_source.get("remote")
        if remote:
            lines.append("")
            lines.append(toml_table(["source", name, "remote"]))
            append_scalar_lines(lines, remote, ["url", "name", "ref"])

    for name, raw_skill in data.get("skill", {}).items():
        if lines:
            lines.append("")
        lines.append(toml_table(["skill", name]))
        append_scalar_lines(lines, raw_skill, ["include", "exclude"])

    return "\n".join(lines).rstrip() + "\n"
