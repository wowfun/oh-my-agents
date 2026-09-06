from __future__ import annotations

import ipaddress
import json
import os
import posixpath
import re
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from hagency_cli.files.sync.models import (
    CONFIG_RELATIVE_PATH,
    DEFAULT_SFTP_CONFIG,
    PROTECTED_CONFIG_PATTERN,
    TEMPORARY_CONFIG_SOURCE,
    FileSyncConfigError,
    FileSyncError,
    LocalSyncSelection,
    Progress,
    RemoteEndpoint,
    SFTPConfig,
    SyncOptions,
    _require_mapping,
)


def render_default_sftp_config() -> str:
    return json.dumps(DEFAULT_SFTP_CONFIG, indent=4) + "\n"


def initialize_sftp_config(
    project_root: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> Path:
    emit = progress or (lambda _message: None)
    root = Path(os.path.abspath(project_root))
    if not root.exists():
        raise FileSyncConfigError(f"project directory does not exist: {root}")
    if not root.is_dir():
        raise FileSyncConfigError(f"project path is not a directory: {root}")

    config_path = root / CONFIG_RELATIVE_PATH
    config_dir = config_path.parent
    if os.path.lexists(config_dir) and not config_dir.is_dir():
        raise FileSyncConfigError(f"config directory is not a directory: {config_dir}")

    config_exists = os.path.lexists(config_path)
    if config_exists and config_path.is_dir():
        raise FileSyncConfigError(f"config path is not a file: {config_path}")
    if config_exists and not force:
        raise FileSyncConfigError(f"SFTP config already exists: {config_path}")

    content = render_default_sftp_config()
    action = "overwrite" if config_exists else "create"
    if dry_run:
        emit(f"Would {action} SFTP config: {config_path}")
        emit(content.rstrip())
        return config_path

    file_descriptor = -1
    temporary_path: Path | None = None
    try:
        config_dir.mkdir(exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{config_path.name}.hgc-", dir=config_dir
        )
        temporary_path = Path(temporary_name)
        handle = os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n")
        file_descriptor = -1
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, config_path)
    except OSError as exc:
        raise FileSyncError(f"cannot write SFTP config {config_path}: {exc}") from exc
    finally:
        if file_descriptor >= 0:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass

    emit(f"initialized SFTP config: {config_path}")
    return config_path


def _load_json(path: Path) -> object:
    if not path.exists():
        raise FileSyncConfigError(f"missing config: {path}")
    if not path.is_file():
        raise FileSyncConfigError(f"config is not a file: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FileSyncConfigError(f"cannot read {path}: {exc}") from exc


def _selection_choices(configs: list[dict]) -> list[str]:
    choices: list[str] = []
    for index, config in enumerate(configs, start=1):
        name = config.get("name")
        base = name if isinstance(name, str) and name else f"config-{index}"
        choices.append(base)
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            choices.extend(f"{base}:{profile}" for profile in profiles)
    return choices


def _merge_profile(base: dict, profile_name: str) -> dict:
    profiles = _require_mapping(base.get("profiles", {}), "profiles")
    if profile_name not in profiles:
        raise FileSyncConfigError(f"unknown SFTP profile: {profile_name}")
    profile = _require_mapping(profiles[profile_name], f"profiles.{profile_name}")
    merged = {key: value for key, value in base.items() if key != "profiles"}
    if "ignore" in profile:
        base_ignore = merged.get("ignore") or []
        profile_ignore = profile.get("ignore") or []
        if not isinstance(base_ignore, list) or not isinstance(profile_ignore, list):
            raise FileSyncConfigError("ignore must be an array of strings")
        merged["ignore"] = [*base_ignore, *profile_ignore]
    merged.update({key: value for key, value in profile.items() if key != "ignore"})
    return merged


def _select_config(raw: object, profile: str | None) -> tuple[dict, str]:
    raw_configs = raw if isinstance(raw, list) else [raw]
    if not raw_configs:
        raise FileSyncConfigError("sftp.json must contain at least one config")
    configs = [
        _require_mapping(value, f"config[{index}]")
        for index, value in enumerate(raw_configs)
    ]
    choices = _selection_choices(configs)

    if profile is None:
        if len(configs) != 1:
            raise FileSyncConfigError(
                "multiple SFTP configs found; select one with --profile "
                f"({', '.join(choices)})"
            )
        selected = configs[0]
        name = selected.get("name")
        selection = name if isinstance(name, str) and name else "config-1"
        default_profile = selected.get("defaultProfile")
        if default_profile is not None:
            if not isinstance(default_profile, str) or not default_profile:
                raise FileSyncConfigError("defaultProfile must be a non-empty string")
            selected = _merge_profile(selected, default_profile)
            selection = f"{selection}:{default_profile}"
        return selected, selection

    matches: list[tuple[dict, str, str]] = []
    explicit_base, separator, explicit_nested = profile.partition(":")
    for index, config in enumerate(configs, start=1):
        name_value = config.get("name")
        name = (
            name_value
            if isinstance(name_value, str) and name_value
            else f"config-{index}"
        )
        if separator:
            if name == explicit_base:
                if explicit_nested:
                    profiles = config.get("profiles")
                    if isinstance(profiles, dict) and explicit_nested in profiles:
                        matches.append(
                            (
                                _merge_profile(config, explicit_nested),
                                profile,
                                profile,
                            )
                        )
                else:
                    matches.append((config, name, f"{name}:"))
            continue
        if name == profile:
            selected = config
            default_profile = config.get("defaultProfile")
            selection = name
            if default_profile is not None:
                if not isinstance(default_profile, str) or not default_profile:
                    raise FileSyncConfigError(
                        "defaultProfile must be a non-empty string"
                    )
                selected = _merge_profile(config, default_profile)
                selection = f"{name}:{default_profile}"
            matches.append((selected, selection, f"{name}:"))
        profiles = config.get("profiles")
        if isinstance(profiles, dict) and profile in profiles:
            nested_selection = f"{name}:{profile}"
            matches.append(
                (_merge_profile(config, profile), nested_selection, nested_selection)
            )

    if len(matches) == 1:
        selected, selection, _selector = matches[0]
        return selected, selection
    if not matches:
        raise FileSyncConfigError(
            f"unknown SFTP profile {profile!r}; choose from: {', '.join(choices)}"
        )
    raise FileSyncConfigError(
        f"ambiguous SFTP profile {profile!r}; use CONFIG: for a base config or "
        f"CONFIG:PROFILE for a nested profile "
        f"({', '.join(selector for _config, _selection, selector in matches)})"
    )


def _optional_string(config: dict, key: str) -> str | None:
    value = config.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise FileSyncConfigError(f"{key} must be a string")
    return value


def _bool_value(config: dict, key: str, default: bool = False) -> bool:
    value = config.get(key, default)
    if not isinstance(value, bool):
        raise FileSyncConfigError(f"{key} must be true or false")
    return value


def _positive_int(config: dict, key: str) -> int | None:
    value = config.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FileSyncConfigError(f"{key} must be a positive integer")
    return value


def _permission(config: dict, key: str) -> int | None:
    value = config.get(key)
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise FileSyncConfigError(f"{key} must be an octal permission such as 644")
    try:
        parsed = int(str(value), 8)
    except ValueError as exc:
        raise FileSyncConfigError(
            f"{key} must be an octal permission such as 644"
        ) from exc
    if parsed < 0 or parsed > 0o7777:
        raise FileSyncConfigError(f"{key} is outside the supported permission range")
    return parsed


def _resolve_local_path(value: str, base: Path) -> Path:
    path = Path(os.path.expanduser(value))
    if not path.is_absolute():
        path = base / path
    return Path(os.path.abspath(path))


def _normalize_remote_path(value: str) -> str:
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return normalized or "."


def parse_remote_endpoint(value: str) -> RemoteEndpoint:
    if not value or value != value.strip():
        raise FileSyncConfigError(
            "remote endpoint must not be empty or have surrounding whitespace"
        )
    if any(character in value for character in ("\0", "\r", "\n")):
        raise FileSyncConfigError("remote endpoint contains an invalid character")

    username: str | None = None
    if "[" in value or "]" in value:
        match = re.fullmatch(
            r"(?:(?P<user>[^@:\s]+)@)?\[(?P<host>[^\[\]\s]+)\]:(?P<path>.*)", value
        )
        if match is None:
            raise FileSyncConfigError(
                "invalid bracketed remote endpoint; expected [IPv6]:path or "
                "user@[IPv6]:path"
            )
        username = match.group("user")
        host = match.group("host")
        remote_path = match.group("path")
        try:
            ipaddress.IPv6Address(host)
        except ValueError as exc:
            raise FileSyncConfigError(
                f"invalid IPv6 address in remote endpoint: {host}"
            ) from exc
    else:
        possible_authority, possible_separator, _possible_path = value.rpartition(":")
        possible_host = possible_authority.rpartition("@")[2]
        if possible_separator:
            try:
                ipaddress.IPv6Address(possible_host)
            except ValueError:
                pass
            else:
                raise FileSyncConfigError(
                    "IPv6 addresses in remote endpoints must be bracketed"
                )
        authority, separator, remote_path = value.partition(":")
        if not separator:
            raise FileSyncConfigError(
                "remote endpoint must use [user@]host:path syntax"
            )
        if authority.count("@") > 1:
            raise FileSyncConfigError("remote endpoint has an invalid user or host")
        username_value, user_separator, host_value = authority.rpartition("@")
        if user_separator:
            username = username_value
            host = host_value
        else:
            host = authority
        if not host or any(character.isspace() for character in host):
            raise FileSyncConfigError("remote endpoint host must not be empty")
        if not re.fullmatch(r"[^:/@\[\]\s]+", host):
            raise FileSyncConfigError(
                "invalid remote endpoint host; bracket IPv6 addresses"
            )
        if username is not None and (
            not username or any(character.isspace() for character in username)
        ):
            raise FileSyncConfigError("remote endpoint username must not be empty")

    if not remote_path:
        raise FileSyncConfigError(
            "remote endpoint path must not be empty; use host:. for the home directory"
        )
    if remote_path.startswith("~/"):
        # SFTP starts in the authenticated user's home directory but does not
        # require the server to expand shell-style tildes.
        remote_path = remote_path[2:] or "."
        if remote_path.startswith("/"):
            raise FileSyncConfigError(
                "remote endpoint home-relative paths must start with exactly ~/"
            )
    elif remote_path.startswith("~"):
        raise FileSyncConfigError(
            "remote endpoint supports only . or ~/path for home-relative paths"
        )
    return RemoteEndpoint(
        host=host,
        remote_path=_normalize_remote_path(remote_path),
        username=username,
    )


def build_temporary_sftp_config(
    local_root: Path,
    endpoint: str | RemoteEndpoint,
    *,
    port: int | None = None,
    identity: Path | None = None,
    exclude: Sequence[str] = (),
    sync_options: SyncOptions | None = None,
) -> SFTPConfig:
    root = Path(os.path.abspath(local_root))
    parsed = parse_remote_endpoint(endpoint) if isinstance(endpoint, str) else endpoint
    if port is not None and (
        isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535
    ):
        raise FileSyncConfigError("port must be between 1 and 65535")
    if any(not isinstance(pattern, str) for pattern in exclude):
        raise FileSyncConfigError("exclude patterns must be strings")

    identity_path = (
        _resolve_local_path(os.fspath(identity), Path.cwd())
        if identity is not None
        else None
    )
    # Built-ins remain last so user negations cannot re-include metadata or keys.
    patterns = (*exclude, ".git", PROTECTED_CONFIG_PATTERN)
    return SFTPConfig(
        config_path=None,
        workspace_root=root,
        selection=TEMPORARY_CONFIG_SOURCE,
        name=TEMPORARY_CONFIG_SOURCE,
        local_root=root,
        remote_root=parsed.remote_path,
        host=parsed.host,
        port=port,
        username=parsed.username,
        password=None,
        private_key_path=identity_path,
        passphrase=None,
        agent=None,
        ssh_config_path=_resolve_local_path("~/.ssh/config", root),
        connect_timeout=10.0,
        remote_time_offset=0.0,
        file_perm=None,
        dir_perm=None,
        use_temp_file=False,
        open_ssh=False,
        sync_options=sync_options or SyncOptions(),
        ignore_patterns=patterns,
        protected_paths=(PurePosixPath(CONFIG_RELATIVE_PATH.as_posix()),),
    )


def load_sftp_config(workspace_root: Path, profile: str | None = None) -> SFTPConfig:
    root = Path(os.path.abspath(workspace_root))
    config_path = root / CONFIG_RELATIVE_PATH
    selected, selection = _select_config(_load_json(config_path), profile)

    name = _optional_string(selected, "name") or selection
    protocol = _optional_string(selected, "protocol") or "sftp"
    if protocol != "sftp":
        raise FileSyncConfigError(
            f"unsupported protocol {protocol!r}; file sync currently supports SFTP"
        )
    host = _optional_string(selected, "host")
    if not host:
        raise FileSyncConfigError("host is required")

    port = _positive_int(selected, "port")
    if port is not None and port > 65535:
        raise FileSyncConfigError("port must be between 1 and 65535")

    timeout_ms = selected.get("connectTimeout", 10_000)
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int | float)
        or timeout_ms <= 0
    ):
        raise FileSyncConfigError("connectTimeout must be a positive number")

    offset = selected.get("remoteTimeOffsetInHours", 0)
    if isinstance(offset, bool) or not isinstance(offset, int | float):
        raise FileSyncConfigError("remoteTimeOffsetInHours must be a number")

    context = _optional_string(selected, "context") or ""
    local_root = _resolve_local_path(context, root)
    remote_path = _optional_string(selected, "remotePath") or "./"

    ignore = selected.get("ignore") or []
    if not isinstance(ignore, list) or any(
        not isinstance(item, str) for item in ignore
    ):
        raise FileSyncConfigError("ignore must be an array of strings")
    patterns = [*ignore]
    ignore_file = _optional_string(selected, "ignoreFile")
    if ignore_file:
        ignore_path = _resolve_local_path(ignore_file, root)
        if not ignore_path.is_file():
            raise FileSyncConfigError(f"ignoreFile not found: {ignore_path}")
        try:
            patterns.extend(ignore_path.read_text(encoding="utf-8-sig").splitlines())
        except (OSError, UnicodeError) as exc:
            raise FileSyncConfigError(
                f"cannot read ignoreFile {ignore_path}: {exc}"
            ) from exc
    # Keep this last so user negations cannot re-include the usual config path.
    # protected_paths also covers a config inside a custom context.
    patterns.append(PROTECTED_CONFIG_PATTERN)

    protected_paths: list[PurePosixPath] = [
        PurePosixPath(CONFIG_RELATIVE_PATH.as_posix())
    ]
    try:
        config_relative = config_path.relative_to(local_root)
    except ValueError:
        pass
    else:
        protected_paths.append(PurePosixPath(config_relative.as_posix()))

    sync_raw = selected.get("syncOption") or {}
    sync_mapping = _require_mapping(sync_raw, "syncOption")
    sync_options = SyncOptions(
        delete=_bool_value(sync_mapping, "delete"),
        skip_create=_bool_value(sync_mapping, "skipCreate"),
        ignore_existing=_bool_value(sync_mapping, "ignoreExisting"),
        update=_bool_value(sync_mapping, "update"),
    )

    private_key = _optional_string(selected, "privateKeyPath")
    passphrase = selected.get("passphrase")
    if (
        passphrase is not None
        and passphrase is not True
        and not isinstance(passphrase, str)
    ):
        raise FileSyncConfigError("passphrase must be a string or true")
    ssh_config = _optional_string(selected, "sshConfigPath") or "~/.ssh/config"
    use_temp_file = _bool_value(selected, "useTempFile")
    open_ssh = _bool_value(selected, "openSsh")
    if open_ssh and not use_temp_file:
        raise FileSyncConfigError("openSsh requires useTempFile to be true")

    return SFTPConfig(
        config_path=config_path,
        workspace_root=root,
        selection=selection,
        name=name,
        local_root=local_root,
        remote_root=_normalize_remote_path(remote_path),
        host=host,
        port=port,
        username=_optional_string(selected, "username"),
        password=_optional_string(selected, "password"),
        private_key_path=(
            _resolve_local_path(private_key, root) if private_key else None
        ),
        passphrase=passphrase,
        agent=_optional_string(selected, "agent"),
        ssh_config_path=_resolve_local_path(ssh_config, root),
        connect_timeout=float(timeout_ms) / 1000,
        remote_time_offset=float(offset) * 3600,
        file_perm=_permission(selected, "filePerm"),
        dir_perm=_permission(selected, "dirPerm"),
        use_temp_file=use_temp_file,
        open_ssh=open_ssh,
        sync_options=sync_options,
        ignore_patterns=tuple(patterns),
        protected_paths=tuple(dict.fromkeys(protected_paths)),
    )


def load_local_sync_selection(
    project_root: Path,
    *,
    profile: str | None = None,
    no_config: bool = False,
    exclude: Sequence[str] = (),
    output_path: Path | None = None,
) -> LocalSyncSelection:
    root = Path(os.path.abspath(project_root))
    config_path = root / CONFIG_RELATIVE_PATH
    if no_config and profile is not None:
        raise FileSyncConfigError("--no-config and --profile are mutually exclusive")
    if any(not isinstance(pattern, str) for pattern in exclude):
        raise FileSyncConfigError("exclude patterns must be strings")

    patterns: list[str] = []
    config_protected_patterns: list[str] = []
    protected_paths: list[PurePosixPath] = [
        PurePosixPath(CONFIG_RELATIVE_PATH.as_posix())
    ]
    selection = "standalone"
    local_root = root

    if not no_config and os.path.lexists(config_path):
        selected, selection = _select_config(_load_json(config_path), profile)
        context = _optional_string(selected, "context") or ""
        local_root = _resolve_local_path(context, root)
        ignore = selected.get("ignore") or []
        if not isinstance(ignore, list) or any(
            not isinstance(item, str) for item in ignore
        ):
            raise FileSyncConfigError("ignore must be an array of strings")
        patterns.extend(ignore)
        ignore_file = _optional_string(selected, "ignoreFile")
        if ignore_file:
            ignore_path = _resolve_local_path(ignore_file, root)
            if not ignore_path.is_file():
                raise FileSyncConfigError(f"ignoreFile not found: {ignore_path}")
            try:
                patterns.extend(
                    ignore_path.read_text(encoding="utf-8-sig").splitlines()
                )
            except (OSError, UnicodeError) as exc:
                raise FileSyncConfigError(
                    f"cannot read ignoreFile {ignore_path}: {exc}"
                ) from exc
        try:
            config_relative = config_path.relative_to(local_root)
        except ValueError:
            pass
        else:
            relative_config_path = PurePosixPath(config_relative.as_posix())
            protected_paths.append(relative_config_path)
            config_protected_patterns.append(f"/{relative_config_path.as_posix()}")
    elif profile is not None:
        raise FileSyncConfigError(
            f"cannot use --profile without SFTP config: {config_path}"
        )

    patterns.extend(exclude)
    output_protected_patterns: list[str] = []
    if output_path is not None:
        absolute_output = Path(os.path.abspath(output_path))
        try:
            output_relative = absolute_output.relative_to(local_root)
        except ValueError:
            pass
        else:
            if output_relative.parts:
                relative_output_path = PurePosixPath(output_relative.as_posix())
                protected_paths.append(relative_output_path)
                output_protected_patterns.append(f"/{relative_output_path.as_posix()}")

    # Keep hard exclusions last so negation rules cannot re-include them.
    patterns.extend(
        (
            ".git",
            PROTECTED_CONFIG_PATTERN,
            *config_protected_patterns,
            *output_protected_patterns,
        )
    )

    return LocalSyncSelection(
        project_root=root,
        local_root=local_root,
        selection=selection,
        ignore_patterns=tuple(patterns),
        protected_paths=tuple(dict.fromkeys(protected_paths)),
    )
