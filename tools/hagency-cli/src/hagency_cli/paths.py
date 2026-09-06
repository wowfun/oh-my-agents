from __future__ import annotations

import os
import re
from pathlib import Path


def normalize_windows_shell_path(value: str) -> str:
    if os.name != "nt":
        return value
    match = re.fullmatch(r"/([A-Za-z])(?:/(.*))?", value)
    if not match:
        return value
    drive = match.group(1).upper()
    rest = match.group(2)
    if not rest:
        return f"{drive}:/"
    return f"{drive}:/{rest}"


def expand_path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(normalize_windows_shell_path(value)))
    if not path.is_absolute():
        path = base / path
    return path
