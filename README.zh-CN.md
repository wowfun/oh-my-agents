# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Hagency CLI

`hgc` CLI 用于管理 Hagency workspace、source、skill discovery 和安装、profile、生成的 profile skill 输出，以及项目构建产物清理。Source registry 位于 [`hagency-config.toml`](hagency-config.toml)，profile config 位于 `profiles/<name>/config.toml`。

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

可为当前 shell 安装补全，或输出指定 shell 的补全脚本：

```sh
hgc --install-completion
hgc --show-completion bash
```

补全覆盖命令、别名、选项、目录，以及本地可用的 source、profile、skill 和 selector 值。它会尊重当前目录、`--root` 和 `--checkout-dir`；workspace 缺失、配置损坏、目录不可读或 source 未同步时会静默省略动态候选。

### 项目构建产物清理

`hgc space purge` 会查找可重建的项目产物，例如依赖目录、构建输出、测试与工具缓存，以及带有有效 `CACHEDIR.TAG` 的目录。先用预览模式检查候选项：

```sh
hgc space purge --dry-run
hgc space purge
hgc space purge ~/Work/client-a ~/scratch/project-b
hgc space purge --paths
```

位置参数 `PATH...` 只作为本次调用的临时扫描根目录，并会取代已保存的路径列表和自动发现结果。未传位置参数时，非空的用户路径文件会取代自动发现；文件缺失或只包含空白和注释时，命令恢复使用自动根目录。单独运行 `--paths` 可创建或编辑该文件，显示已配置根目录的存在状态，并在编辑器退出后重新显示生效列表。编辑器按 `$VISUAL`、`$EDITOR`、Windows 上的 `notepad.exe`、macOS 上的 `open -W -t` 或其他平台上的 `vi` 依次选择。空行和 `#` 注释会被忽略，每行必须是绝对路径或 `~` 路径。

路径文件在 Linux 和 WSL 上位于 `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths`，在 macOS 上位于 `~/Library/Application Support/Hagency/space-purge-paths`，在 Windows 上位于 `%APPDATA%\Hagency\space-purge-paths`。`APPDATA` 不可用时，Windows 回退到 `~/.config/hagency/space-purge-paths`。

自动发现会检查已存在的 `~/www`、`~/dev`、`~/Projects`、`~/GitHub`、`~/Code`、`~/Workspace`、`~/Repos`、`~/Development`、`~/.codex/worktrees` 和 `~/.claude/worktrees`，也会扫描主目录的直接子目录，只纳入两层深度内存在项目标记的容器。系统目录和云存储根目录不会被自动发现。

交互式 TTY 中，Questionary 会显示多选列表。产物目录及其后代文件的最后活动时间严格超过 7 天时，候选项默认选中；较新或无法确定时间的候选项保留在列表中，但不会预选。常规清理会输出每个已选项的绝对路径，再请求二次确认，且默认回答为否。使用 `--dry-run` 时仍会打开多选列表，但只预览已选项，并跳过破坏性的二次确认。非 TTY 环境会跳过交互界面，预览所有候选项及其默认选中状态，即使没有传入 `--dry-run` 也不会删除文件。

该命令会永久删除已选产物，不会将它们移到废纸篓或回收站。Git 已跟踪的候选项（包括内部嵌套 Git 仓库含有 tracked 文件时的整个候选项）、符号链接、junction、reparse path、挂载点、大小为零的候选项、Xcode 的全局 `DerivedData` 目录、非 Composer 项目中的 `vendor` 目录，以及非 `.NET` 项目中的 `bin` 目录都会被排除。通用产物名只在有项目标记支持时才会成为候选项。如果永久删除因权限、文件系统变化或其他 I/O 错误而中途失败，部分内容可能已经被删除；命令会报告失败、继续处理后续选项，并以状态码 1 退出。

### 本地模型代理

`hgc serve start --model-proxy` 会启动后台进程，并为每个 provider 同时提供 OpenAI Responses 和 Chat Completions 接口。Provider 只由 URL 选择，绝不根据 `model` 路由；`model` 值原样发给上游。

在 `hagency-config.toml` 旁创建 `hagency-model-proxy.toml`：

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

环境变量值会先从 `hagency-model-proxy.toml` 同目录的 `.env` 读取，再由进程环境中的同名变量覆盖。受信任的 Hook 也可以通过只读的 `init.env` 访问合并结果，因此 provider 的特殊认证无需自行加载文件。不要将 `.env` 提交到版本控制。

OpenAI-compatible 客户端可使用以下 base URL：

```text
http://127.0.0.1:8765/v1
http://127.0.0.1:8765/openai/v1
http://127.0.0.1:8765/corp/v1
```

使用同一个 workspace root 或显式配置路径管理后台进程：

```sh
hgc serve start --model-proxy -r <workspace>
hgc serve stop --model-proxy -r <workspace>
hgc serve restart --model-proxy -r <workspace>
```

Linux 使用 detached session；Windows 使用 detached、无控制台窗口的独立进程组。状态和日志在 Linux 下写入 `XDG_STATE_HOME`/`~/.local/state`，在 Windows 下写入 `LOCALAPPDATA`；可将 `HAGENCY_STATE_HOME` 设为绝对路径来覆盖根目录。`start` 会输出准确日志路径；日志达到 10 MiB 时轮转，并保留三份备份。

不带 provider 的 `/v1` 路由使用 `default_provider`；`/<provider>/v1` 显式选择 provider。`POST /responses`、`POST /chat/completions` 与 `GET /models` 无需客户端额外配置即可使用：上游协议匹配时走原始实体透传路径，另一接口执行转换。`/models` 会代理 adapter 的模型列表操作，并执行相同的请求、认证和响应 Hook 阶段；若 provider 没有标准模型列表接口，Hook 也可以实现 `fetch_models(ctx)` 并返回模型 ID 字符串。由于该精简契约不含创建时间，合成的模型记录使用稳定的未知值 `created = 0`。原生协议族下的额外资源操作会继续透传，但不会跨协议模拟。

下游凭证 header 默认剥离。需要逐 provider 传递时使用 `forward_credential_headers`；普通认证使用静态/env header；复杂签名或协议方言可在 `<config-dir>/hooks/` 下添加可信 Python Hook。Hook 通过只读的 `init.env` 获取合并后的环境变量，在进程内运行，修改后需重启。定义 `process_response` 后，非 SSE 响应会先完整缓冲再调用 Hook，并受 64 MiB 响应上限约束；无需检查响应时应省略该方法。服务只接受 loopback 监听地址。

`adapter = "openai"` 会提供 Responses 协议和 OpenAI API 根地址；`adapter = "openai_compatible"` 默认使用 Chat Completions，并要求填写 `base_url`。需要时可在 provider 级覆盖 `protocol`。新增 provider 家族时，只需在 [`model_proxy/providers`](tools/hagency-cli/src/hagency_cli/model_proxy/providers/README.md) 下新增一个导出 `ADAPTER` 的模块；文件名就是 adapter 值，不需要修改中央注册表。

`[defaults].depth` 设置默认 sync 深度；临时性的 Git 网络失败会自动重试。失败后可用 `hgc source sync -s <slice>` 继续同步指定 source 范围。Git URL 推断出的 repo 名已存在时，`source add` 会 fallback 到 `owner/repo`；也可以传 `--name` 自定义 source 名。

同一份配置需要在不同平台使用不同 checkout 目录时，可以添加 Windows 覆盖值：

```toml
[defaults]
checkout_dir = "~/Projects/references"
checkout_dir_windows = "/d/Projects/references"
```

Checkout 目录的优先级是 `--checkout-dir`，然后是原生 Windows 上的 `checkout_dir_windows`，最后是 `checkout_dir`。WSL 不属于原生 Windows，仍使用 `checkout_dir`。在原生 Windows 上，Git Bash 路径 `/d/Projects/references` 会被规范化为 `D:/Projects/references`。如果未设置 `checkout_dir_windows`，原生 Windows 会回退到 `checkout_dir`。这是配置覆盖，不会新增 CLI 参数。

常规同步会拒绝非快进更新。如果上游 source 重写历史，且该 checkout 可丢弃，可用 `--reanchor` 重试失败的选择范围。重锚仅接受没有暂存、未暂存或未跟踪更改的 checkout，并用拉取到的上游历史替换 local-only commits。该选项支持 source 名、`--profile` 和 `--slice`；`--dry-run` 只描述满足条件时的行为。

`skill add` 通过唯一 skill 名或精确的 `SOURCE:selector` 安装一个已发现的 skill。默认链接到调用目录的 `.agents/skills`。`-p/--path` 直接指定最终 skills 容器，命令只会在其后追加 skill 名；`-d/--dir` 指定 workspace 根目录，安装目标为 `<workspace>/.agents/skills`；`--global` 安装到 `~/.agents/skills`。这三个选项互斥。相对目标路径以调用目录为基准，`~` 展开为当前用户的主目录。`--root` 和 `--checkout-dir` 只影响 skill discovery，不会改变安装目标。非 Windows 平台使用 symlink，Windows 使用 junction。

`profile init` 必须且只能选择一个目标：`-p/--path` 是最终 skills 容器，`-d/--dir` 是 workspace 根目录，对应目标为 `<workspace>/.agents/skills`。目标路径同样以调用目录为基准并支持 `~` 展开，`--root` 和 `--checkout-dir` 仍只用于 discovery。这一版本更改了 `-p` 的原有语义：请将 `hgc p init -p <root> <profile>` 改为 `hgc p init -d <root> <profile>`。

如果发现多个安装名称相同的 skill 目录，交互式终端会要求为本次初始化选择一个来源路径，且不会改写 profile。非交互安装会失败，并提示改在终端中运行或收窄 profile 的 `include` selector；使用 `--dry-run` 时则会列出每个冲突来源并继续预览，不替用户做选择。

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | 使用 Hagency Kit CLI 管理 source、profile、skill、项目构建产物清理、profile 初始化或本地模型代理 | 帮助 Agent 管理 Hagency workspace 内容，安全预览项目构建产物清理，并运行 provider 级 Responses/Chat 代理接口。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

Profile 在 `profiles/<name>/config.toml` 中声明要启用的 source 名和 skill selector。初始化后，选中的 skills 会被物化到指定的 skills 容器；`-d/--dir` 使用 workspace 下的 `.agents/skills` 容器。
