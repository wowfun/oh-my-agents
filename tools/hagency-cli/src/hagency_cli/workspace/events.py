from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class OperationEvent:
    message: str
    error: bool = False


Progress = Callable[[OperationEvent], None]


def emit_event(progress: Progress | None, message: str, *, error: bool = False) -> None:
    if progress is not None:
        progress(OperationEvent(message, error))
