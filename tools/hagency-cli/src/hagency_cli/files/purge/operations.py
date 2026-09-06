from __future__ import annotations

from hagency_cli.files.purge.models import (
    ItemDisposition,
    PurgeDisposition,
    PurgeIssue,
    PurgeItemResult,
    PurgeReport,
    PurgeRequest,
    PurgeUI,
    _PlannedCandidate,
    _PurgePlan,
)
from hagency_cli.files.purge.removal import _permanently_remove, _revalidate
from hagency_cli.files.purge.scan import _build_plan


def _partial_or(
    disposition: PurgeDisposition,
    issues: tuple[PurgeIssue, ...],
    results: tuple[PurgeItemResult, ...] = (),
) -> PurgeDisposition:
    if disposition in {PurgeDisposition.PREVIEW, PurgeDisposition.CANCELLED}:
        return disposition
    if any(issue.is_failure for issue in issues) or any(
        result.disposition in {ItemDisposition.SKIPPED, ItemDisposition.FAILED}
        for result in results
    ):
        return PurgeDisposition.PARTIAL
    return disposition


def _selection_known_bytes(selected: tuple[_PlannedCandidate, ...]) -> int:
    seen: set[tuple[int, int]] = set()
    total = sum(
        candidate.choice.size_bytes or 0
        for candidate in selected
        if candidate.choice.size_bytes is not None
    )
    for candidate in selected:
        if candidate.choice.size_bytes is None:
            continue
        for entry in candidate.hardlink_entries:
            if entry.identity in seen:
                total -= entry.size_bytes
            else:
                seen.add(entry.identity)
    return total


def _make_report(
    plan: _PurgePlan,
    disposition: PurgeDisposition,
    *,
    selected: tuple[_PlannedCandidate, ...] = (),
    results: tuple[PurgeItemResult, ...] = (),
    extra_issues: tuple[PurgeIssue, ...] = (),
) -> PurgeReport:
    issues = (*plan.issues, *extra_issues)
    return PurgeReport(
        disposition=_partial_or(disposition, issues, results),
        roots=plan.roots,
        choices=tuple(item.choice for item in plan.candidates),
        selected_paths=tuple(item.choice.exact_path for item in selected),
        results=results,
        issues=issues,
        known_bytes=_selection_known_bytes(selected),
    )


def _bind_selection(
    ids: tuple[str, ...], plan: _PurgePlan
) -> tuple[tuple[_PlannedCandidate, ...], tuple[PurgeIssue, ...]]:
    by_id = {candidate.choice.id: candidate for candidate in plan.candidates}
    if len(ids) != len(set(ids)):
        return (), (
            PurgeIssue("invalid_selection", None, "selection contains duplicate IDs"),
        )
    unknown = [value for value in ids if value not in by_id]
    if unknown:
        return (), (
            PurgeIssue(
                "invalid_selection",
                None,
                f"selection contains unknown IDs: {', '.join(unknown)}",
            ),
        )
    return tuple(by_id[value] for value in ids), ()


def purge_space(request: PurgeRequest, *, ui: PurgeUI) -> PurgeReport:
    plan = _build_plan(request)
    interactive = ui.is_interactive()

    if not interactive:
        selected = tuple(item for item in plan.candidates if item.choice.preselected)
        results = tuple(
            PurgeItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "non-interactive preview",
            )
            for item in selected
        )
        return _make_report(
            plan, PurgeDisposition.PREVIEW, selected=selected, results=results
        )

    if not plan.candidates:
        return _make_report(plan, PurgeDisposition.COMPLETED)

    selected_ids = ui.select(tuple(item.choice for item in plan.candidates))
    if selected_ids is None or not selected_ids:
        return _make_report(plan, PurgeDisposition.CANCELLED)
    selected, selection_issues = _bind_selection(selected_ids, plan)
    if selection_issues:
        return _make_report(
            plan,
            PurgeDisposition.PARTIAL,
            extra_issues=selection_issues,
        )

    if request.dry_run:
        results = tuple(
            PurgeItemResult(
                item.choice.exact_path,
                ItemDisposition.WOULD_REMOVE,
                item.choice.size_bytes,
                "dry-run preview",
            )
            for item in selected
        )
        return _make_report(
            plan, PurgeDisposition.PREVIEW, selected=selected, results=results
        )

    known_bytes = _selection_known_bytes(selected)
    exact_paths = tuple(item.choice.exact_path for item in selected)
    if not ui.confirm_exact(exact_paths, known_bytes):
        return _make_report(plan, PurgeDisposition.CANCELLED, selected=selected)

    results: list[PurgeItemResult] = []
    for item in selected:
        reason = _revalidate(item)
        if reason is not None:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.SKIPPED,
                    item.choice.size_bytes,
                    reason,
                )
            )
            continue
        try:
            _permanently_remove(item)
        except OSError as exc:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.FAILED,
                    item.choice.size_bytes,
                    str(exc),
                )
            )
        else:
            results.append(
                PurgeItemResult(
                    item.choice.exact_path,
                    ItemDisposition.REMOVED,
                    item.choice.size_bytes,
                )
            )
    result_tuple = tuple(results)
    return _make_report(
        plan,
        PurgeDisposition.COMPLETED,
        selected=selected,
        results=result_tuple,
    )
