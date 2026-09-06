from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path

from hagency_cli.files.purge.inspection import (
    _canonical_key,
    _context_allows,
    _discover_git_contexts,
    _find_project_root,
    _git_tracked_state,
    _identity,
    _is_reparse_stat,
    _is_within,
    _measure_candidate,
    _same_identity,
    _valid_cachedir_tag,
)
from hagency_cli.files.purge.models import (
    CACHEDIR_TAG_NAME,
    MAX_SCAN_DEPTH,
    MIN_SCAN_DEPTH,
    PURGE_TARGETS,
    SCAN_PRUNE_NAMES_CASEFOLD,
    Activity,
    PurgeChoice,
    PurgeIssue,
    PurgeRequest,
    _Identity,
    _PlannedCandidate,
    _PurgePlan,
)
from hagency_cli.files.purge.roots import _resolve_roots


def _candidate_id(path: Path, identity: _Identity) -> str:
    payload = f"{path}\0{identity.device}\0{identity.inode}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _scan_root(
    root: Path,
    now: float,
) -> tuple[list[_PlannedCandidate], list[PurgeIssue]]:
    candidates: list[_PlannedCandidate] = []
    issues: list[PurgeIssue] = []
    try:
        root_identity = _identity(root)
    except OSError as exc:
        return [], [
            PurgeIssue("root_stat_failed", root, f"could not inspect root: {exc}")
        ]

    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as entries:
                entry_list = list(entries)
        except OSError as exc:
            issues.append(
                PurgeIssue("scan_failed", directory, f"could not scan directory: {exc}")
            )
            continue

        for entry in entry_list:
            path = Path(entry.path)
            child_depth = depth + 1
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as exc:
                issues.append(
                    PurgeIssue(
                        "scan_stat_failed", path, f"could not inspect path: {exc}"
                    )
                )
                continue
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _is_reparse_stat(info)
            ):
                continue
            if entry.name.casefold() in SCAN_PRUNE_NAMES_CASEFOLD:
                continue

            named_target = entry.name in PURGE_TARGETS
            tagged_target = _valid_cachedir_tag(path)
            if child_depth >= MIN_SCAN_DEPTH and (named_target or tagged_target):
                project_root = _find_project_root(path, root)
                if project_root is not None and _context_allows(path, project_root):
                    git_contexts, git_error = _discover_git_contexts(path)
                    if git_error is not None:
                        issues.append(
                            PurgeIssue(
                                "git_check_failed",
                                path,
                                f"could not prove candidate is untracked: {git_error}",
                            )
                        )
                    else:
                        tracked: bool | None = False
                        for git_context in git_contexts:
                            tracked_path = (
                                path
                                if _is_within(
                                    path,
                                    git_context.root,
                                    allow_equal=True,
                                )
                                else git_context.root
                            )
                            tracked, git_error = _git_tracked_state(
                                tracked_path, git_context.root
                            )
                            if tracked is None or tracked:
                                break
                        if tracked is None:
                            issues.append(
                                PurgeIssue(
                                    "git_check_failed",
                                    path,
                                    "could not prove candidate is untracked: "
                                    f"{git_error}",
                                )
                            )
                            continue
                        if tracked:
                            continue
                        try:
                            parent_identity = _identity(path.parent)
                            target_identity = _identity(path)
                        except OSError as exc:
                            issues.append(
                                PurgeIssue(
                                    "candidate_stat_failed",
                                    path,
                                    f"could not bind candidate identity: {exc}",
                                )
                            )
                        else:
                            (
                                size_bytes,
                                activity,
                                measure_error,
                                hardlink_entries,
                            ) = _measure_candidate(path, now)
                            if measure_error is not None:
                                issues.append(
                                    PurgeIssue(
                                        "candidate_measure_failed",
                                        path,
                                        measure_error,
                                    )
                                )
                            if size_bytes != 0:
                                choice = PurgeChoice(
                                    id=_candidate_id(path, target_identity),
                                    exact_path=path.resolve(),
                                    project_path=project_root.resolve(),
                                    artifact_kind=entry.name
                                    if named_target
                                    else CACHEDIR_TAG_NAME,
                                    size_bytes=size_bytes,
                                    activity=activity,
                                    preselected=activity is Activity.OLD
                                    and size_bytes is not None,
                                )
                                candidates.append(
                                    _PlannedCandidate(
                                        choice=choice,
                                        root=root,
                                        root_identity=root_identity,
                                        parent_identity=parent_identity,
                                        target_identity=target_identity,
                                        git_contexts=git_contexts,
                                        hardlink_entries=hardlink_entries,
                                    )
                                )
                # Never descend into a known artifact tree, even when protected.
                continue

            if child_depth < MAX_SCAN_DEPTH:
                stack.append((path, child_depth))
    if not _same_identity(root, root_identity):
        return [], [
            *issues,
            PurgeIssue(
                "root_changed_during_scan",
                root,
                "scan root changed before its results could be published",
            ),
        ]
    stable_candidates: list[_PlannedCandidate] = []
    for candidate in candidates:
        if _same_identity(
            candidate.choice.exact_path.parent, candidate.parent_identity
        ) and _same_identity(candidate.choice.exact_path, candidate.target_identity):
            stable_candidates.append(candidate)
        else:
            issues.append(
                PurgeIssue(
                    "candidate_changed_during_scan",
                    candidate.choice.exact_path,
                    "candidate or its parent changed before scan results were published",
                )
            )
    return stable_candidates, issues


def _drop_duplicate_and_nested(
    candidates: list[_PlannedCandidate],
) -> list[_PlannedCandidate]:
    ordered = sorted(
        candidates,
        key=lambda item: (
            len(item.choice.exact_path.parts),
            str(item.choice.exact_path),
        ),
    )
    kept: list[_PlannedCandidate] = []
    identities: set[tuple[int, int] | str] = set()
    for candidate in ordered:
        identity: tuple[int, int] | str = (
            (candidate.target_identity.device, candidate.target_identity.inode)
            if candidate.target_identity.inode
            else _canonical_key(candidate.choice.exact_path)
        )
        if identity in identities:
            continue
        if any(
            _is_within(candidate.choice.exact_path, item.choice.exact_path)
            for item in kept
        ):
            continue
        identities.add(identity)
        kept.append(candidate)
    return kept


def _sort_candidates(
    candidates: list[_PlannedCandidate],
) -> tuple[_PlannedCandidate, ...]:
    project_totals: dict[Path, int] = {}
    seen_hardlinks: set[tuple[int, int]] = set()
    for candidate in sorted(candidates, key=lambda item: str(item.choice.exact_path)):
        project = candidate.choice.project_path
        accounted_size = candidate.choice.size_bytes or 0
        for entry in candidate.hardlink_entries:
            if entry.identity is None:
                continue
            if entry.identity in seen_hardlinks:
                accounted_size -= entry.size_bytes
            else:
                seen_hardlinks.add(entry.identity)
        project_totals[project] = project_totals.get(project, 0) + max(
            accounted_size, 0
        )
    return tuple(
        sorted(
            candidates,
            key=lambda item: (
                -project_totals[item.choice.project_path],
                str(item.choice.project_path),
                -(item.choice.size_bytes if item.choice.size_bytes is not None else -1),
                str(item.choice.exact_path),
            ),
        )
    )


def _build_plan(request: PurgeRequest) -> _PurgePlan:
    roots, root_issues = _resolve_roots(request)
    now = time.time()
    candidates: list[_PlannedCandidate] = []
    issues = list(root_issues)
    for root in roots:
        found, scan_issues = _scan_root(root, now)
        candidates.extend(found)
        issues.extend(scan_issues)
    candidates = _drop_duplicate_and_nested(candidates)
    return _PurgePlan(roots, _sort_candidates(candidates), tuple(issues))
