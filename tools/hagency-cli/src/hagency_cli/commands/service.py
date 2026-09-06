from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated

import typer

from hagency_cli.commands.completion import complete_directory, complete_file
from hagency_cli.commands.shared import (
    command_errors,
    die,
    make_app,
    require_at_most_one,
)
from hagency_cli.model_proxy import ModelProxyConfigError
from hagency_cli.model_proxy.daemon import (
    ModelProxyServiceError,
    restart_model_proxy,
    start_model_proxy,
    stop_model_proxy,
)
from hagency_cli.paths import expand_path
from hagency_cli.workspace.context import workspace_root_arg

service_app = make_app(
    help_text="""Manage local background services.

    model-proxy exposes Responses and Chat Completions endpoints on a loopback
    IP address. Configure providers before starting it; start and restart print
    the worker PID, listen address, and log path after the worker binds its port.

    \b
    Examples:
      hgc service model-proxy --help
      hgc service model-proxy start --config ./hagency-model-proxy.toml
    """,
    add_completion=False,
)
model_proxy_app = make_app(
    help_text="""Manage the local model proxy.

    Uses hagency-model-proxy.toml in the Hagency workspace, resolved via --root,
    current-directory ancestors, then the editable-installed Hagency Kit checkout.
    Alternatively, --config selects a file relative to the invocation directory
    and needs no workspace. --config and --root are mutually exclusive.
    Use the same resolved config path for start, stop, and restart.

    The default listener is 127.0.0.1:8765. Clients use /v1 for the default
    provider or /PROVIDER/v1 for an explicit provider, with /responses,
    /chat/completions, and GET /models under either base URL. Provider selection
    uses the URL; model values pass through unchanged.

    Create a config with version = 1, default_provider, and provider tables.
    Environment-backed values read .env beside the config; process environment
    values take precedence. The CLI README documents provider and hook options.

    \b
    Minimal hagency-model-proxy.toml:
      version = 1
      default_provider = "openai"
      [providers.openai]
      adapter = "openai"
      api_key = { env = "OPENAI_API_KEY" }

    \b
    Examples:
      hgc service model-proxy start --config ./hagency-model-proxy.toml
      hgc service model-proxy stop --config ./hagency-model-proxy.toml
      hgc service model-proxy restart --help
    """,
    add_completion=False,
)
service_app.add_typer(model_proxy_app, name="model-proxy")


def model_proxy_config_path(*, root: str | None, config: str | None) -> Path:
    require_at_most_one({"--root": root, "--config": config})
    if config is not None:
        return expand_path(config, Path.cwd())
    return workspace_root_arg(root) / "hagency-model-proxy.toml"


def validate_model_proxy_host(host: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        ) from exc
    if not address.is_loopback:
        raise typer.BadParameter(
            "host must be a loopback IP address", param_hint="--host"
        )


def model_proxy_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def start_model_proxy_command(
    *,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    validate_model_proxy_host(host)
    try:
        state, paths = start_model_proxy(config_path, host=host, port=port)
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"started model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")


def stop_model_proxy_command(*, root: str | None, config: str | None) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    try:
        stopped, _paths = stop_model_proxy(config_path)
    except ModelProxyServiceError as exc:
        die(str(exc))
    print("stopped model proxy" if stopped else "model proxy is not running")


def restart_model_proxy_command(
    *,
    root: str | None,
    config: str | None,
    host: str,
    port: int,
) -> None:
    config_path = model_proxy_config_path(root=root, config=config)
    validate_model_proxy_host(host)
    try:
        state, paths = restart_model_proxy(config_path, host=host, port=port)
    except (ModelProxyConfigError, ModelProxyServiceError) as exc:
        die(str(exc))
    print(
        f"restarted model proxy: pid {state.pid}, "
        f"{model_proxy_url(state.host, state.port)}"
    )
    print(f"log: {paths.log}")


@model_proxy_app.command("start", short_help="Start the model proxy in the background.")
@command_errors
def serve_start_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_file,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
) -> None:
    """Start the model proxy in the background and wait for its listener.

    Requires a configured hagency-model-proxy.toml in the resolved workspace, or
    an explicit --config file relative to the invocation directory. --config and
    --root are mutually exclusive. Provider credentials may use .env beside the
    config; process environment wins. See hgc service model-proxy --help for a
    minimal config and the CLI README for full provider/hook configuration.

    Binds 127.0.0.1:8765 by default; --host must be a loopback IP address. Clients
    use http://127.0.0.1:8765/v1 or /PROVIDER/v1, exposing /responses,
    /chat/completions, and GET /models. Model values pass to the selected provider
    unchanged. Config and hooks are loaded on startup, without hot reload.

    Prints PID, listen address, and log path after the worker binds. An already
    running service for this config is an error; use restart to reload it. Read
    the printed log for runtime failures. State/logs use the platform's user state
    directory; absolute HAGENCY_STATE_HOME overrides it. Logs rotate at 10 MiB
    with three backups. Use the same resolved config path when stopping.

    \b
    Examples:
      hgc service model-proxy start --root ./kit
      hgc service model-proxy start --config ./hagency-model-proxy.toml --port 8766
    """
    start_model_proxy_command(root=root, config=config, host=host, port=port)


@model_proxy_app.command("stop", short_help="Stop the background model proxy.")
@command_errors
def serve_stop_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_file,
        ),
    ] = None,
) -> None:
    """Stop the background worker identified by its resolved config path.

    Defaults to hagency-model-proxy.toml in the resolved Hagency workspace.
    --config instead selects a path relative to the invocation directory and
    needs no workspace; it cannot accompany --root. Use the same resolved config
    path and state directory as start. Absolute HAGENCY_STATE_HOME overrides the
    platform's user state directory. Stopping an already stopped worker succeeds
    and reports that it is not running; configuration and logs remain in place.

    \b
    Examples:
      hgc service model-proxy stop --root ./kit
      hgc service model-proxy stop --config ./hagency-model-proxy.toml
    """
    stop_model_proxy_command(root=root, config=config)


@model_proxy_app.command("restart", short_help="Restart the background model proxy.")
@command_errors
def serve_restart_cli(
    root: Annotated[
        str | None,
        typer.Option(
            "--root",
            "-r",
            help="Workspace with hagency-config.toml; defaults to current-directory ancestors, then editable-installed checkout",
            autocompletion=complete_directory,
        ),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option(
            "--config",
            help="Model proxy TOML config path",
            autocompletion=complete_file,
        ),
    ] = None,
    host: Annotated[
        str, typer.Option("--host", help="Loopback IP address to listen on")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", min=1, max=65535, help="TCP port to listen on")
    ] = 8765,
) -> None:
    """Stop the identified proxy worker, then start it with current settings.

    Uses hagency-model-proxy.toml in the resolved workspace or an explicit --config
    path relative to the invocation directory. --config and --root are mutually
    exclusive. Use the same resolved config path and state directory as the old
    worker. An absolute HAGENCY_STATE_HOME overrides the platform state directory.

    Reloads config, adjacent .env, and hooks; process environment overrides .env.
    A stopped worker is started. Listener options default to 127.0.0.1:8765 on each
    invocation, so repeat custom --host/--port values. --host must be a loopback IP.
    If startup fails after stopping, the old worker remains stopped.

    Returns after binding and prints PID, address, and log path. Clients use /v1
    or /PROVIDER/v1 for /responses, /chat/completions, and GET /models. Inspect the
    reported log when diagnosing failures. See hgc service model-proxy --help for
    a minimal config and the CLI README for full provider/hook configuration.

    \b
    Examples:
      hgc service model-proxy restart --root ./kit
      hgc service model-proxy restart --config ./hagency-model-proxy.toml --port 8766
    """
    restart_model_proxy_command(root=root, config=config, host=host, port=port)
