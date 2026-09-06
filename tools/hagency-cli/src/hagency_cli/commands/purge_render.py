from __future__ import annotations

import sys
from collections.abc import Iterable
from typing import Any

from hagency_cli.files.purge.models import PathsEditReport, PurgeReport


def format_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


def _value(value: Any) -> str:
    return str(getattr(value, "value", value)).lower()


def _render_issues(issues: Iterable[Any]) -> None:
    for issue in issues:
        level = "Error" if issue.is_failure else "Warning"
        code = f" [{issue.code}]" if issue.code else ""
        path = f" ({issue.path})" if issue.path is not None else ""
        print(f"{level}{code}{path}: {issue.message}", file=sys.stderr)


def _selected_unknown_count(report: PurgeReport) -> int:
    selected = set(report.selected_paths)
    return sum(
        choice.exact_path in selected and choice.size_bytes is None
        for choice in report.choices
    )


def _size_summary(known_bytes: int, unknown_count: int) -> str:
    summary = f"known size {format_bytes(known_bytes)}"
    if unknown_count:
        suffix = "artifact" if unknown_count == 1 else "artifacts"
        summary += f", {unknown_count} {suffix} with unknown size"
    return summary


def _render_choices(report: PurgeReport) -> None:
    selected = set(report.selected_paths)
    for choice in report.choices:
        marker = "[selected]" if choice.exact_path in selected else "[ ]"
        activity = _value(choice.activity).replace("_", " ")
        size = (
            format_bytes(choice.size_bytes)
            if choice.size_bytes is not None
            else "size unknown"
        )
        print(
            f"{marker} {activity} | {choice.artifact_kind} | {size} | "
            f"{choice.exact_path}"
        )


def _result_prefix(disposition: Any) -> str:
    value = _value(disposition)
    if value in {"preview", "would_remove", "would-remove"}:
        return "Would remove"
    if value in {"removed", "completed"}:
        return "removed"
    if value in {"skipped", "cancelled", "canceled"}:
        return "skipped"
    return "failed"


def _render_results(report: PurgeReport) -> None:
    for result in report.results:
        prefix = _result_prefix(result.disposition)
        size = (
            f" ({format_bytes(result.size_bytes)})"
            if result.size_bytes is not None
            else ""
        )
        message = f" - {result.message}" if result.message else ""
        output = sys.stderr if prefix == "failed" else sys.stdout
        print(f"{prefix}: {result.exact_path}{size}{message}", file=output)


def render_purge_report(report: PurgeReport) -> None:
    _render_issues(report.issues)

    if report.choices:
        _render_choices(report)
    _render_results(report)

    disposition = _value(report.disposition)
    if disposition in {"cancelled", "canceled"}:
        print("Purge cancelled.")
        return
    if not report.choices:
        print("No purge candidates found.")
        return

    selected_count = len(report.selected_paths)
    size = _size_summary(report.known_bytes, _selected_unknown_count(report))
    if disposition == "preview":
        state = "incomplete" if report.failed else "complete"
        print(f"Preview {state}: {selected_count} artifact(s), {size}.")
    elif disposition == "partial":
        print(
            f"Purge partially completed: {selected_count} selected artifact(s), {size}."
        )
    else:
        print(f"Purge complete: {selected_count} selected artifact(s), {size}.")


def render_paths_edit_report(report: PathsEditReport) -> None:
    _render_issues(report.issues)
    if report.failed:
        print(f"Failed to update purge paths: {report.config_path}", file=sys.stderr)
    else:
        action = "unchanged" if report.before_roots == report.after_roots else "updated"
        print(f"Purge paths {action}: {report.config_path}")
        if report.editor:
            print(f"Editor: {report.editor}")

    for label, roots in (
        ("Before", report.before_roots),
        ("After", report.after_roots),
    ):
        print(f"{label}:")
        if not roots:
            print("  (none)")
        for root in roots:
            print(f"  {root}")


__all__ = ["render_paths_edit_report", "render_purge_report"]
