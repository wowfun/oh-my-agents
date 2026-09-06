from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from hagency_cli.files.sync.models import (
    BUNDLE_FORMAT,
    BUNDLE_MANIFEST_MAX_BYTES,
    BUNDLE_MANIFEST_PATH,
    BUNDLE_PAYLOAD_PREFIX,
    BUNDLE_VERSION,
    CONTENT_CHUNK_SIZE,
    PROTECTED_CONFIG_PATTERN,
    BundleEntry,
    BundleManifest,
    BundleMode,
    EntryKind,
    FileSyncConfigError,
    _require_mapping,
)
from hagency_cli.files.sync.selection import IgnoreMatcher, _ignored_bundle_path


def _bundle_payload_path(path: PurePosixPath) -> str:
    return f"{BUNDLE_PAYLOAD_PREFIX}{path.as_posix()}"


def _bundle_manifest_payload(manifest: BundleManifest) -> bytes:
    entries = [
        {
            "path": entry.path.as_posix(),
            "kind": entry.kind.value,
            "size": entry.size,
            "mtime_ns": entry.mtime_ns,
            "mode": entry.mode,
            "sha256": entry.sha256,
        }
        for entry in manifest.entries
    ]
    payload = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "mode": manifest.mode.value,
        "created_at": manifest.created_at,
        "entries": entries,
        "deletions": [path.as_posix() for path in manifest.deletions],
        "ignore": list(manifest.ignore_patterns),
        "skipped_symlinks": [path.as_posix() for path in manifest.skipped_symlinks],
    }
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(encoded) > BUNDLE_MANIFEST_MAX_BYTES:
        raise FileSyncConfigError(
            f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
        )
    return encoded


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


_BUNDLE_MANIFEST_KEYS = frozenset(
    {
        "format",
        "version",
        "mode",
        "created_at",
        "entries",
        "deletions",
        "ignore",
        "skipped_symlinks",
    }
)


_BUNDLE_ENTRY_KEYS = frozenset({"path", "kind", "size", "mtime_ns", "mode", "sha256"})


_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
)


def _strict_mapping_keys(value: object, expected: frozenset[str], label: str) -> dict:
    mapping = _require_mapping(value, label)
    actual = frozenset(mapping)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unknown {', '.join(extra)}")
        raise FileSyncConfigError(f"{label} has invalid fields: {'; '.join(details)}")
    return mapping


def _parse_bundle_path(value: object, label: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise FileSyncConfigError(f"{label} must be a non-empty string")
    if any(character in value for character in ("\\", "\0", "\r", "\n")):
        raise FileSyncConfigError(f"{label} contains an invalid character")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or re.fullmatch(r"[A-Za-z]:.*", path.parts[0]) is not None
    ):
        raise FileSyncConfigError(f"{label} is not a safe relative path: {value}")
    return path


def _validate_windows_bundle_path(path: PurePosixPath) -> None:
    if os.name != "nt":
        return
    for part in path.parts:
        if (
            any(character in '<>:"|?*' or ord(character) < 32 for character in part)
            or part.endswith((" ", "."))
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
        ):
            raise FileSyncConfigError(
                f"bundle path is not valid on Windows: {path.as_posix()}"
            )


def _parse_bundle_manifest(raw: object) -> BundleManifest:
    manifest = _strict_mapping_keys(raw, _BUNDLE_MANIFEST_KEYS, "manifest")
    if manifest["format"] != BUNDLE_FORMAT:
        raise FileSyncConfigError("unsupported sync bundle format")
    version = manifest["version"]
    if isinstance(version, bool) or version != BUNDLE_VERSION:
        raise FileSyncConfigError(f"unsupported sync bundle version: {version!r}")
    try:
        mode = BundleMode(manifest["mode"])
    except (TypeError, ValueError) as exc:
        raise FileSyncConfigError(
            f"unsupported sync bundle mode: {manifest['mode']!r}"
        ) from exc

    created_at = manifest["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise FileSyncConfigError("manifest.created_at must be a UTC timestamp")
    try:
        parsed_time = datetime.fromisoformat(created_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise FileSyncConfigError(
            "manifest.created_at must be a UTC timestamp"
        ) from exc
    if parsed_time.utcoffset() != UTC.utcoffset(parsed_time):
        raise FileSyncConfigError("manifest.created_at must be a UTC timestamp")

    entries_raw = manifest["entries"]
    if not isinstance(entries_raw, list):
        raise FileSyncConfigError("manifest.entries must be an array")
    entries: list[BundleEntry] = []
    entry_paths: set[PurePosixPath] = set()
    for index, raw_entry in enumerate(entries_raw):
        entry = _strict_mapping_keys(
            raw_entry, _BUNDLE_ENTRY_KEYS, f"manifest.entries[{index}]"
        )
        path = _parse_bundle_path(entry["path"], f"manifest.entries[{index}].path")
        _validate_windows_bundle_path(path)
        if path in entry_paths:
            raise FileSyncConfigError(f"duplicate bundle entry path: {path}")
        entry_paths.add(path)
        try:
            kind = EntryKind(entry["kind"])
        except (TypeError, ValueError) as exc:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].kind must be file or directory"
            ) from exc
        if kind not in {EntryKind.FILE, EntryKind.DIRECTORY}:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].kind must be file or directory"
            )
        size = entry["size"]
        mtime_ns = entry["mtime_ns"]
        entry_mode = entry["mode"]
        digest = entry["sha256"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise FileSyncConfigError(
                f"manifest.entries[{index}].size must be a non-negative integer"
            )
        if isinstance(mtime_ns, bool) or not isinstance(mtime_ns, int):
            raise FileSyncConfigError(
                f"manifest.entries[{index}].mtime_ns must be an integer"
            )
        if (
            isinstance(entry_mode, bool)
            or not isinstance(entry_mode, int)
            or not 0 <= entry_mode <= 0o7777
        ):
            raise FileSyncConfigError(f"manifest.entries[{index}].mode is invalid")
        if kind is EntryKind.FILE:
            if (
                not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise FileSyncConfigError(
                    f"manifest.entries[{index}].sha256 is invalid"
                )
        elif size != 0 or digest is not None:
            raise FileSyncConfigError(
                f"manifest.entries[{index}] has invalid directory metadata"
            )
        entries.append(
            BundleEntry(
                path=path,
                kind=kind,
                size=size,
                mtime_ns=mtime_ns,
                mode=entry_mode,
                sha256=digest,
            )
        )

    entries_by_path = {entry.path: entry for entry in entries}
    for entry in entries:
        for parent in entry.path.parents:
            if parent == PurePosixPath("."):
                continue
            parent_entry = entries_by_path.get(parent)
            if parent_entry is None:
                raise FileSyncConfigError(
                    f"bundle entry is missing parent directory: {parent}"
                )
            if parent_entry.kind is not EntryKind.DIRECTORY:
                raise FileSyncConfigError(
                    f"bundle file path is the parent of another entry: {parent}"
                )

    def parse_path_array(key: str) -> tuple[PurePosixPath, ...]:
        values = manifest[key]
        if not isinstance(values, list):
            raise FileSyncConfigError(f"manifest.{key} must be an array")
        paths = tuple(
            _parse_bundle_path(value, f"manifest.{key}[{index}]")
            for index, value in enumerate(values)
        )
        if len(set(paths)) != len(paths):
            raise FileSyncConfigError(f"manifest.{key} contains duplicate paths")
        for path in paths:
            _validate_windows_bundle_path(path)
        return paths

    deletions = parse_path_array("deletions")
    skipped_symlinks = parse_path_array("skipped_symlinks")
    if mode is BundleMode.FULL and deletions:
        raise FileSyncConfigError("full sync bundles cannot contain deletion markers")
    if entry_paths.intersection(deletions):
        raise FileSyncConfigError("bundle entries and deletions overlap")
    if entry_paths.intersection(skipped_symlinks) or set(deletions).intersection(
        skipped_symlinks
    ):
        raise FileSyncConfigError("bundle skipped symlinks overlap managed paths")

    ignore = manifest["ignore"]
    if not isinstance(ignore, list) or any(
        not isinstance(pattern, str) for pattern in ignore
    ):
        raise FileSyncConfigError("manifest.ignore must be an array of strings")

    all_paths = [*entry_paths, *deletions, *skipped_symlinks]
    portable_paths: dict[str, PurePosixPath] = {}
    for path in all_paths:
        key = unicodedata.normalize("NFC", path.as_posix()).casefold()
        previous = portable_paths.get(key)
        if previous is not None and previous != path:
            raise FileSyncConfigError(
                f"bundle paths collide across platforms: {previous} and {path}"
            )
        portable_paths[key] = path

    return BundleManifest(
        mode=mode,
        created_at=created_at,
        entries=tuple(entries),
        deletions=deletions,
        ignore_patterns=tuple(ignore),
        skipped_symlinks=skipped_symlinks,
    )


def _validate_bundle_source(manifest: BundleManifest, ignore: IgnoreMatcher) -> None:
    for entry in manifest.entries:
        if ignore.matches(entry.path, directory=entry.kind is EntryKind.DIRECTORY):
            raise FileSyncConfigError(
                f"bundle entry targets a protected or ignored path: {entry.path}"
            )
    for path in manifest.deletions:
        if _ignored_bundle_path(path, ignore):
            raise FileSyncConfigError(
                f"bundle deletion targets a protected or ignored path: {path}"
            )


def _read_and_verify_bundle(archive: zipfile.ZipFile) -> BundleManifest:
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise FileSyncConfigError("sync bundle contains duplicate ZIP entries")
    if BUNDLE_MANIFEST_PATH not in names:
        raise FileSyncConfigError("sync bundle is missing manifest.json")
    manifest_info = archive.getinfo(BUNDLE_MANIFEST_PATH)
    if manifest_info.file_size > BUNDLE_MANIFEST_MAX_BYTES:
        raise FileSyncConfigError(
            f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
        )
    try:
        with archive.open(manifest_info, "r") as source:
            manifest_payload = source.read(BUNDLE_MANIFEST_MAX_BYTES + 1)
        if len(manifest_payload) > BUNDLE_MANIFEST_MAX_BYTES:
            raise FileSyncConfigError(
                f"sync bundle manifest exceeds {BUNDLE_MANIFEST_MAX_BYTES} bytes"
            )
        raw_manifest = json.loads(manifest_payload.decode("utf-8"))
    except (KeyError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
        raise FileSyncConfigError(f"cannot read sync bundle manifest: {exc}") from exc
    manifest = _parse_bundle_manifest(raw_manifest)
    _validate_bundle_source(
        manifest,
        IgnoreMatcher(
            (*manifest.ignore_patterns, ".git", PROTECTED_CONFIG_PATTERN),
            manifest.skipped_symlinks,
            protect_git=True,
        ),
    )

    expected_names = {BUNDLE_MANIFEST_PATH}
    for entry in manifest.entries:
        if entry.kind is EntryKind.FILE:
            expected_names.add(_bundle_payload_path(entry.path))
    actual_names = set(names)
    if actual_names != expected_names:
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        detail = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if extra:
            detail.append(f"unexpected {', '.join(extra)}")
        raise FileSyncConfigError(
            f"sync bundle payload does not match manifest: {'; '.join(detail)}"
        )

    info_by_name = {info.filename: info for info in infos}
    for entry in manifest.entries:
        if entry.kind is not EntryKind.FILE:
            continue
        payload_path = _bundle_payload_path(entry.path)
        info = info_by_name[payload_path]
        if info.is_dir() or info.file_size != entry.size:
            raise FileSyncConfigError(
                f"sync bundle size mismatch for {entry.path.as_posix()}"
            )
        digest = hashlib.sha256()
        size = 0
        try:
            with archive.open(info, "r") as source:
                while True:
                    chunk = source.read(CONTENT_CHUNK_SIZE)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > entry.size:
                        raise FileSyncConfigError(
                            f"sync bundle size mismatch for {entry.path.as_posix()}"
                        )
                    digest.update(chunk)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise FileSyncConfigError(
                f"cannot verify sync bundle payload {entry.path.as_posix()}: {exc}"
            ) from exc
        if size != entry.size or digest.hexdigest() != entry.sha256:
            raise FileSyncConfigError(
                f"sync bundle checksum mismatch for {entry.path.as_posix()}"
            )
    return manifest


def verify_sync_bundle(bundle_path: Path) -> BundleManifest:
    bundle = Path(os.path.abspath(bundle_path))
    if not bundle.is_file():
        raise FileSyncConfigError(f"sync bundle is not a file: {bundle}")
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            return _read_and_verify_bundle(archive)
    except FileSyncConfigError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise FileSyncConfigError(f"cannot read sync bundle {bundle}: {exc}") from exc
