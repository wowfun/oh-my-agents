# Hagency Kit

Language: English | [简体中文](README.zh-CN.md)

Practical agent skills for reviewing, diagnosing, and operating AI-assisted engineering work.

## Hagency CLI

The `hgc` CLI manages Hagency workspaces, sources, skill discovery and installation, profiles, generated profile skill outputs, and project artifact cleanup. Source registry entries live in [`hagency-config.toml`](hagency-config.toml), and profile configs live under `profiles/<name>/config.toml`.

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace>
hgc skill add <source>:<selector> --global
hgc p init -p <xxx>/skills <profile>
hgc p init -d <workspace> <profile>
hgc space purge --dry-run
hgc serve start --model-proxy
```

Install completion for the current shell, or print a shell-specific completion script:

```sh
hgc --install-completion
hgc --show-completion bash
```

Completion covers commands, aliases, options, directories, and locally available source, profile, skill, and selector values. It respects the current directory, `--root`, and `--checkout-dir`; missing, invalid, unreadable, or unsynced workspace data is silently omitted.

### Project artifact purge

`hgc space purge` finds rebuildable project artifacts such as dependency directories, build output, test and tool caches, and directories carrying a valid `CACHEDIR.TAG`. Start with a preview:

```sh
hgc space purge --dry-run
hgc space purge
hgc space purge ~/Work/client-a ~/scratch/project-b
hgc space purge --paths
```

Positional `PATH...` values are temporary scan roots for that invocation and replace both the saved path list and automatic discovery. Without positional paths, a nonempty per-user path file replaces automatic discovery; a missing or effectively empty file restores the automatic roots. Use `--paths` by itself to create or edit this file, show which configured roots exist, and reopen the effective list after the editor exits. It uses `$VISUAL`, then `$EDITOR`, then `notepad.exe` on Windows, `open -W -t` on macOS, or `vi` elsewhere. Blank lines and `#` comments are ignored, and each entry must be an absolute or `~` path.

The path file is `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths` on Linux and WSL, `~/Library/Application Support/Hagency/space-purge-paths` on macOS, and `%APPDATA%\Hagency\space-purge-paths` on Windows. Windows falls back to `~/.config/hagency/space-purge-paths` when `APPDATA` is unavailable.

Automatic discovery checks existing `~/www`, `~/dev`, `~/Projects`, `~/GitHub`, `~/Code`, `~/Workspace`, `~/Repos`, `~/Development`, `~/.codex/worktrees`, and `~/.claude/worktrees`, plus direct children of the home directory that contain project markers within two levels. It does not automatically search system or cloud-storage roots.

In a TTY, Questionary presents a multi-select list. Candidates whose last artifact activity is strictly more than seven days old are selected by default; recent or uncertain candidates remain unselected. A normal purge then prints every selected absolute path and asks for a second confirmation that defaults to No. With `--dry-run`, the multi-select still opens, but the selected entries are only previewed and the destructive confirmation is skipped. A non-TTY run skips the TUI and previews all candidates with their default selection status, even when `--dry-run` is omitted.

Purge deletes selected artifacts permanently rather than moving them to Trash or the Recycle Bin. It excludes Git-tracked candidates, including an entire candidate that contains a nested Git repository with tracked files, plus symlinks, junctions, reparse paths, mount points, zero-size candidates, the global Xcode `DerivedData` directory, non-Composer `vendor` directories, and `bin` directories outside `.NET` projects. Generic artifact names are accepted only with project evidence. If permanent deletion fails partway through because of permissions, a filesystem change, or another I/O error, some contents may already be gone; the command reports the failure, continues with later selections, and exits with status 1.

### Local model proxy

`hgc serve start --model-proxy` starts a background process that exposes both OpenAI Responses and Chat Completions interfaces for every configured provider. Provider selection comes from the URL, never from `model`; the model value is forwarded unchanged.

Create `hagency-model-proxy.toml` beside `hagency-config.toml`:

```toml
version = 1
default_provider = "openai"

[providers.openai]
adapter = "openai"
api_key = { env = "OPENAI_API_KEY" }

[providers.corp]
adapter = "openai_compatible"
base_url = "https://llm.corp.example/openai/v1"
hook = "corp.py"

[providers.corp.headers]
"X-Tenant" = { env = "CORP_TENANT" }
```

Environment-backed values are resolved from `.env` beside `hagency-model-proxy.toml` and then the process environment, with the process environment taking precedence. The same merged, read-only mapping is available to trusted Hooks as `init.env`, so provider-specific authentication can consume workspace credentials without loading files itself. Keep `.env` out of version control.

Then point an OpenAI-compatible client at one of these base URLs:

```text
http://127.0.0.1:8765/v1
http://127.0.0.1:8765/openai/v1
http://127.0.0.1:8765/corp/v1
```

Manage the background process with the same workspace root or explicit config path:

```sh
hgc serve start --model-proxy -r <workspace>
hgc serve stop --model-proxy -r <workspace>
hgc serve restart --model-proxy -r <workspace>
```

Linux uses a detached session; Windows uses a detached, no-console process group. State and logs are stored below `XDG_STATE_HOME`/`~/.local/state` on Linux and `LOCALAPPDATA` on Windows. Set `HAGENCY_STATE_HOME` to an absolute path to override that root. `start` reports the exact log path; logs rotate at 10 MiB with three backups.

The bare `/v1` routes use `default_provider`; `/<provider>/v1` selects one explicitly. `POST /responses`, `POST /chat/completions`, and `GET /models` are available without per-client configuration. A matching upstream protocol uses a raw entity path; the other interface is converted. `/models` proxies the adapter's model-list operation and uses the same request, authentication, and response Hook stages. A Hook may instead implement `fetch_models(ctx)` and return model ID strings when the provider has no standard model-list endpoint; because that compact contract contains no creation metadata, synthesized model records use `created = 0` as a stable unknown value. Additional resource operations under the native protocol family are passed through without cross-protocol emulation.

Downstream credential headers are stripped by default. Use `forward_credential_headers` for explicit per-provider forwarding, static/env headers for normal authentication, or a trusted Python hook under `<config-dir>/hooks/` for custom request, signing, and response handling. Hooks receive the merged environment as the read-only `init.env` mapping, run in-process, and take effect after a restart. Defining `process_response` buffers non-SSE responses before invoking the hook and enforces a 64 MiB response limit; omit that method when response inspection is unnecessary. The server only accepts loopback listen addresses.

`adapter = "openai"` supplies the Responses protocol and OpenAI API root; `adapter = "openai_compatible"` defaults to Chat Completions and requires `base_url`. Override `protocol` at provider level when needed. To add a provider family, add one module under [`model_proxy/providers`](tools/hagency-cli/src/hagency_cli/model_proxy/providers/README.md) that exports `ADAPTER`; the filename becomes the adapter value and no central registry change is needed.

`[defaults].depth` sets the default sync depth; transient Git network failures are retried automatically. Use `hgc source sync -s <slice>` to resume a selected source range after a failure. When a Git URL's inferred repo name already exists, `source add` falls back to `owner/repo`; pass `--name` to choose a custom source name.

Use an optional Windows checkout override when the same config is shared across platforms:

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Checkout directory precedence is `--checkout-dir`, then `checkout_dir_windows` on native Windows, then `checkout_dir`. WSL is not treated as native Windows and continues to use `checkout_dir`. On native Windows, Git Bash paths such as `/d/Projects/references` are normalized to `D:/Projects/references`. If `checkout_dir_windows` is omitted, native Windows falls back to `checkout_dir`. This is a config-only override; there is no new CLI flag.

Normal sync refuses non-fast-forward updates. If an upstream source rewrites history and its checkout is disposable, rerun the failed selection with `--reanchor`. Reanchoring requires a checkout with no staged, unstaged, or untracked changes, then replaces local-only commits with the fetched upstream history. The option works with source names, `--profile`, and `--slice`; `--dry-run` only describes the conditional behavior.

`skill add` installs one discovered skill by unique name or exact `SOURCE:selector`. It links into the invocation directory's `.agents/skills` by default. Use `-p/--path` for an exact skills container, to which only the skill name is appended; use `-d/--dir` for a workspace root whose destination is `<workspace>/.agents/skills`; or use `--global` for `~/.agents/skills`. These three options are mutually exclusive. Relative destination paths resolve against the invocation directory, and `~` expands to the current user's home. `--root` and `--checkout-dir` affect skill discovery only; they never change the installation destination. Non-Windows platforms use symlinks; Windows uses junctions.

`profile init` requires exactly one destination: `-p/--path` is the final skills container and `-d/--dir` is a workspace root that expands to `<workspace>/.agents/skills`. Destination paths follow the same invocation-directory and `~` expansion rules, while `--root` and `--checkout-dir` remain discovery-only. This changes the previous `-p` behavior. Migrate `hgc p init -p <root> <profile>` to `hgc p init -d <root> <profile>`.

If multiple discovered skill directories have the same install name, an interactive terminal prompts you to choose the source path for that initialization. The profile is not rewritten. A non-interactive installation fails with guidance to rerun in a terminal or narrow the profile's `include` selector; with `--dry-run`, it lists every conflicting source candidate and continues the preview without choosing one.

## Skills

| Skill | When | What it does |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | Explaining git diffs, commit ranges, branch comparisons, or pasted changesets | Turns raw change evidence into release-oriented summaries, feature change lists, risk notes, testing gaps, and draft release notes. |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | Auditing prompts, agent workflows, toolchains, multi-agent systems, or production readiness | Scores workflow health across prompts, context, tools, architecture, safety, reliability, and system performance using available evidence. |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | Using the Hagency Kit CLI for sources, profiles, skills, project artifact cleanup, profile initialization, or the local model proxy | Helps agents manage Hagency workspace content, safely preview project artifact cleanup, and run provider-level Responses/Chat proxy endpoints. |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | Investigating application, server, JSON, CI, or rotated gzip logs | Samples and analyzes logs to explain failures, error spikes, slow requests, traffic patterns, and incident signals while keeping evidence bounded and redacted. |

## Profiles

A profile is a lightweight bundle definition for an agent workflow scene.

A profile lists the source names and skill selectors it enables in `profiles/<name>/config.toml`. After initialization, selected skills are materialized in the requested skills container; `-d/--dir` uses the workspace's `.agents/skills` container.
