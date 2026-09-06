# Hagency Kit

语言：简体中文 | [English](README.md)

实用 Agent 技能，用于审阅、诊断和维护 AI 辅助工程工作。

## Hagency CLI

`hgc` CLI 用于管理 Hagency workspace、source、skill discovery 和安装、profile、在线与离线项目文件同步、生成的 profile skill 输出，以及项目构建产物清理。Source registry 位于 [`hagency-config.toml`](hagency-config.toml)，profile config 位于 `profiles/<name>/config.toml`。

```sh
uv tool install -e tools/hagency-cli
hgc s add <git-url> --sync
hgc s sync --profile <profile>
hgc skill ls
hgc skill add <skill>
hgc skill add <skill> -p <xxx>/skills
hgc skill add <source>:<selector> -d <workspace>
hgc skill add <source>:<selector> --global
hgc p apply -p <xxx>/skills <profile>
hgc p apply -d <workspace> <profile>
hgc file sync --dry-run
hgc file purge --dry-run
hgc service model-proxy start
```

使用 editable 安装后，`hgc` 在 workspace 外运行时可以定位到提供
`hagency_cli` 源码的 Hagency Kit checkout，因此通常不再需要 `-r`。Workspace
优先级依次为显式 `--root`、当前目录及其父目录、安装源码所在 checkout。
Fallback 只检查该源码 checkout 根目录中的配置，不会沿已安装包的祖先目录
继续查找。非 editable 安装在 workspace 外运行时需要指定 `--root`。

可为当前 shell 安装补全，或输出指定 shell 的补全脚本：

```sh
hgc --install-completion
hgc --show-completion bash
```

补全覆盖命令、别名、选项、目录，以及本地可用的 source、profile、skill 和 selector 值。它与命令使用相同的 workspace 优先级，并尊重 `--checkout-dir`；workspace 缺失、配置损坏、目录不可读或 source 未同步时会静默省略动态候选。

### 命令与迁移

```text
hgc init
hgc source  list|show|add|remove|sync
hgc skill   list|add
hgc profile list|show|add|update|remove|apply
hgc file    init|push|pull|sync|pack|apply|purge
hgc service model-proxy start|stop|restart
```

`source sync` 更新长期保留的 Git checkout，`file sync` 通过 SFTP 同步项目文件。继续支持 `s`/`p`、`ls`、`rm` 和 profile 的 `u` 别名。

| 已删除的命令 | 替代命令 |
| --- | --- |
| `hgc sync local-to-remote` / `l2r` | `hgc file push` |
| `hgc sync remote-to-local` / `r2l` | `hgc file pull` |
| `hgc sync both` | `hgc file sync` |
| `hgc sync init\|pack\|apply` | `hgc file init\|pack\|apply` |
| `hgc space purge` | `hgc file purge` |
| `hgc profile init` / `hgc p init` | `hgc profile apply` / `hgc p apply` |
| `hgc serve ACTION --model-proxy` | `hgc service model-proxy ACTION` |

旧入口不再保留。现有操作参数沿用原语义；文件排除模式应重复传入 `--exclude`，安装 selector 应重复传入 `--skill`。旧式多值 `-i`/`-e` 展开仅用于 `profile`/`p add|update|u`。配置格式、bundle v1、代理状态位置和 `space-purge-paths` 文件名保持不变。模块职责与验证命令见 [CLI 架构说明](tools/hagency-cli/README.md)。

### 快捷安装技能

```sh
hgc skill add owner/repo --skill code-review --skill tools/testing
hgc skill add https://github.com/owner/repo.git --ref main --all
hgc skill add ./local-skills --all
hgc skill add existing-source --skill nested/one --dry-run
```

快捷安装仍要求能解析到 Hagency 工作区，不会隐式初始化。输入支持已发现技能名、精确的 `SOURCE:selector`、已登记来源名、Git URL、GitHub `owner/repo` 和显式本地目录（`./`、`../`、`~`、绝对路径及 Windows 路径）。已登记来源及其 selector 优先于 GitHub 简写，本地路径按调用目录解析。

`--skill/-s` 可重复指定来源内 selector，也可使用 `--all`；两者互斥，精确单技能输入不能叠加筛选。Selector 必须留在来源目录内，绝对路径、越界的 `..` 及指向来源外部的 symlink 会被拒绝。只发现一个技能时直接安装；多个技能且没有筛选时，TTY 多选默认不选，非 TTY 必须提供 selector 或 `--all`。取消或空选择不安装。包括 `--all` 在内，所有选择及同名冲突都在首次写入安装目标前解决。

远程输入复用 URL 和显式 ref 匹配的唯一来源。同一传输协议的 GitHub URL 统一末尾 `/` 与 `.git`，其他 URL 精确匹配，不推断 HTTPS 与 SSH 等价；多项匹配要求 `--source-name`。新远程来源先使用仓库名，重名时尝试 `owner/repo`，仍冲突则要求显式名称。本地目录复用包含它的最具体来源，以 workspace 为后备，并限定在输入子目录内发现；并列来源要求指定名称，否则新本地来源默认用目录名。新名称不能是 `workspace`，也不能包含路径逃逸或引用歧义。

新远程来源使用工作区默认 ref/depth。仅获取缺失 checkout，不自动修改已有来源的 URL、ref、分支或内容；显式 ref 与实际 checkout 不符时，需先执行 `source sync`。未登记目录占用新 checkout 路径时，会在登记前报错。新远程来源的 `--checkout-dir` 会保存为实际解析后的 checkout 路径，已有来源则只影响本次路径发现。静态校验后按登记、获取、选择、安装执行；获取失败或取消会保留登记及已取得的 checkout，进度输出说明已完成阶段，安装不承诺回滚。

技能安装的 `--dry-run` 不联网、不写配置或目标；checkout 尚未取得时，只输出拟登记与获取步骤，并明确候选和安装计划尚未验证。已有 checkout 的显式 ref 会通过只读 Git 命令校验；位于远程 checkout 内的本地子目录也支持 `--ref`。`skill list/ls` 继续列出可发现技能，不是已安装清单。

### SFTP 文件同步

先在已存在的项目目录中初始化参考 VSCode-SFTP 模板，再编辑其中的连接占位值。初始化不会连接服务器，也不会写入密码或私钥。已有配置默认拒绝覆盖，只有传入 `--force` 才会替换；`--dry-run` 只打印目标路径和模板，不创建文件：

```sh
hgc file init --root /path/to/project
hgc file init --root /path/to/project --dry-run
hgc file init --root /path/to/project --force
```

在包含 `.vscode/sftp.json` 的项目目录中运行同步。命令沿用 VSCode-SFTP 的 `context` 到 `remotePath` 映射：

```sh
hgc file push --dry-run
hgc file push
hgc file push --git-changed --dry-run
hgc file pull
hgc file sync
```

`push` 上传，`pull` 下载，`sync` 双向同步。

一次性目录树同步可直接传入 SCP 风格 endpoint。此模式默认以当前目录为本地根目录，也可通过 `--root` 指定；即使 `.vscode/sftp.json` 存在或已经损坏，也会完全绕过它：

```sh
hgc file push dev@server:/srv/project --dry-run
hgc file pull server:~/Projects/ws --root ./restore
hgc file sync server:C:/Projects/ws --exclude '*.tmp'
hgc file push '[2001:db8::1]:/srv/project' -P 2222 -i ~/.ssh/id_ed25519
```

endpoint 必须包含非空远端路径；远端主目录使用 `host:.`，并支持 `host:~/path`、SSH alias、显式 `user@host`、方括号包裹的 IPv6 和 Windows 盘符路径。三个临时同步方向都接受 `-P/--port`、`-i/--identity`、可重复的 `--exclude`、`--skip-create` 和 `--ignore-existing`。两个单向命令还接受 `--delete` 和 `--update`，`push` 额外接受 `--git-changed`；`sync` 不开放 `--delete` 或 `--update`。这些临时专用选项必须与 endpoint 一起使用，endpoint 与 `--profile` 互斥。

临时同步采用默认不删除的安全配置，并始终排除 `.git` 文件或目录，以及各级子项目的 `.vscode/sftp.json`；用户后续提供否定规则也不能重新纳入它们。CLI 不提供明文密码参数。认证使用 SSH agent、默认密钥、SSH config 或 `--identity`；加密私钥应先载入 agent。endpoint 中的用户、`-P`、`-i` 优先于 SSH config；默认 `~/.ssh/config` 中的 `HostName`、`User`、`Port`、`IdentityFile` 和 `ProxyCommand` 仍会生效。最后回退到端口 22 和当前系统用户，已有的 host-key 校验继续生效。

`push` 和 `pull` 将命令中指定的一侧视为来源，并让它在共有路径冲突时胜出。默认会复制目标侧缺失的文件，并用整秒级修改时间和大小找出可能需要覆盖的文件；只存在于目标侧的路径会保留。覆盖共有普通文件前，如果大小已能证明 CRLF 归一化后也不可能相等，命令会跳过内容比较；否则读取两侧内容，字节完全相同，或文本仅有 CRLF 与 LF 换行差异时，会跳过该动作。包含 NUL 字节的文件按二进制原始内容比较，单独的 CR 仍视为有效差异，实际传输永远不会改写来源字节。dry-run 同样会进行这一步额外的内容读取。`syncOption.update`、`ignoreExisting`、`skipCreate` 和 `delete` 保持 VSCode-SFTP 语义；`delete = true` 会删除只存在于目标侧的路径，因此应先检查 dry-run 计划。`sync` 会把单侧文件复制到另一侧，并让共有文件中精确修改时间较新的版本胜出；时间完全相同时本地侧胜出。与 VSCode-SFTP 一致，此模式只使用 `skipCreate` 和 `ignoreExisting`。

**检测限制：** 同一修改时间秒内、大小不变的编辑可能被漏过，`--git-changed` 也不例外；它只筛选路径，不会强制比较内容。目前没有 checksum/force 选项。完成摘要统计的是计划动作数，不是实际改动文件数；删除已经不存在的路径或包含受保护内容的非空目录时，文件系统可能保持不变。

使用 `hgc file push --git-changed` 可将上传范围限制为配置中本地 `context`（临时模式则是本地根目录）下 Git 报告的 staged、unstaged、untracked、deleted 和 renamed 路径；Git ignored 路径不纳入。重命名会上传新路径；与其他删除一样，移除旧远端路径在配置模式下要求 `syncOption.delete = true`，在临时模式下要求 `--delete`。Git 路径集合只会缩小正常同步计划，ignore 和 sync 选项仍拥有最终决定权。扫描只保留变更路径及其父目录，在规划前跳过无关子目录；仍需枚举所访问目录中的条目。如果没有 Git 变化，命令不会建立 SFTP 连接。普通同步完全不会探测 Git，因此未安装 Git、项目为普通目录都不受影响；只有显式传入 `--git-changed` 时，Git 缺失、`context` 不在仓库中或 Git 检查失败才会在连接远端前明确报错。每条 Git 检查命令的超时为 120 秒。

在线同步使用单个 SFTP 连接，按顺序执行传输。当前支持 SFTP 配置、`context`、Windows 风格远端路径、`ignore`、`ignoreFile`、`remoteTimeOffsetInHours`、`filePerm`、`dirPerm`、`useTempFile`/`openSsh`，以及密码、私钥、SSH agent 和 `~/.ssh/config` 认证。配置权限只在创建或上传远端文件、目录时应用，不会校准既有远端目录的权限。所有在线同步模式的来源和目标扫描均强制排除 `.git` 文件、目录以及各级子项目的 `.vscode/sftp.json`，包括大小写变体；用户否定规则也无法重新纳入这些元数据。Git worktree 和 submodule 的 `.git` 文件不会被同步。SSH host key 必须已存在于用户的 known-hosts 文件中。单个配置会被自动选择；配置数组或嵌套 `profiles` 可用 `--profile NAME` 选择，有歧义的短名称会被拒绝，`--profile CONFIG:PROFILE` 明确选择嵌套 profile，`--profile CONFIG:` 则选择不应用 `defaultProfile` 的基础配置；其他情况下 `defaultProfile` 会自动生效。需要指定其他项目目录时使用 `--root <directory>`。除了没有变化的 `--git-changed` 上传外，`--dry-run` 仍会连接并扫描远端，但不会修改任何文件。

### 离线同步包

无法使用 SSH、SFTP 或网络时，可使用 `pack` / `apply`。`pack` 只读取本地文件并生成跨平台 ZIP；用户通过任意外部渠道传递 ZIP 后，再在目标端本地校验并应用：

```sh
hgc file pack --root /path/to/source
hgc file pack -r /path/to/source -o release.zip --force
hgc file pack -r /path/to/source --git-changed --exclude '*.tmp'
hgc file apply release.zip --root /path/to/destination --dry-run
hgc file apply release.zip -r /path/to/destination --delete
```

默认输出为调用目录下的 `hgc-sync.zip`。已有输出默认拒绝覆盖，只有显式传入 `--force` 才会通过同目录临时文件和原子替换写入。`--dry-run` 会读取并计算所选来源文件的摘要，但不会创建包。来源默认为当前目录。存在 `.vscode/sftp.json` 时，`pack` 只复用 profile 选择、`context`、`ignore` 和 `ignoreFile`；它既不会验证或记录连接、认证字段，也绝不会建立 SFTP 连接。缺少配置时按普通目录处理。JSON 损坏或 profile 选择有歧义会报错；`--no-config` 可完全绕过配置探测，并与 `--profile` 互斥。

普通 pack 生成完整来源快照；`--git-changed` 则生成 Git patch，包含 staged、unstaged、untracked、deleted 和 renamed 路径，重命名前的旧路径会成为删除标记。Git clean 或过滤后没有有效变化时不会创建包。完整 pack 和普通目录不需要安装 Git。Git 元数据（`.git` 文件或目录）、各级子项目的 `.vscode/sftp.json`、输出包自身、配置 ignore 规则和可重复的 `--exclude` 始终排除。符号链接不会跟随或打包；它们会记录到清单并打印醒目警告，但 pack 仍成功结束。

Git patch 的打包和应用会在扫描时跳过无关子目录。ZIP 根目录包含版本化 `manifest.json`，文件原始字节位于 `payload/`。清单解压后的大小上限为 16 MiB，超限时会在解压或写入目标前拒绝；打包及其 dry-run 遵守相同限制。该上限只约束清单，不限制 payload 解压后的总大小。清单记录可移植相对路径、类型、大小、纳秒 mtime、POSIX mode、SHA-256、有效 ignore 规则、删除标记和跳过的符号链接，但不含 host、凭证或来源绝对路径。SHA-256 只用于检测损坏；同步包不提供签名、来源认证或加密。

同步包会先校验清单，拒绝指向 Git 元数据、任意深度的 SFTP 配置或清单排除路径的条目和删除标记，再读取 payload。否定规则无法覆盖元数据保护，大小写变体也受到保护。`apply` 会在任何目标写入前完整验证归档、清单版本、条目集合、安全路径、重复项、跨平台路径冲突、大小和摘要。目标目录可以不存在，只会在真实 apply 时创建。默认由包内容赢得共有路径冲突；`--skip-create`、`--ignore-existing` 和 `--update` 沿用单向同步语义。对于 full 包，`--delete` 删除目标独有且未忽略的路径；对于 Git patch，它只应用清单中的明确删除标记，绝不会扩张成目标目录镜像。文件写入采用原子替换，删除最后执行。文本仅有 CRLF/LF 差异时保持目标字节不变，含 NUL 文件按原始字节比较，传输内容永不改写。POSIX 上新文件恢复来源 mode、覆盖文件保留目标 mode；Windows 不强制 POSIX 权限；支持的平台会恢复 mtime。

### 通过 SFTP 同步远端 WSL

如果 Windows OpenSSH alias 只是通过 `RemoteCommand` 启动 WSL，它仍然是 Windows SFTP endpoint：OpenSSH 将 SFTP 配置为独立 subsystem，`hgc` 不会解析 `RemoteCommand` 或 `RequestTTY`。要远程同步 WSL 文件系统，需要在目标 WSL 发行版内运行标准 sshd，并通过可直达地址或 Windows 端口转发/防火墙暴露，再为它设置独立 alias（[Microsoft OpenSSH Server configuration](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)）：

```sshconfig
Host win-wsl
  HostName desktop-7divr3r
  User <wsl-linux-user>
  Port 2222
```

```sh
hgc file push win-wsl:/home/<wsl-linux-user>/Projects/ws --dry-run
hgc file pull win-wsl:~/Projects/ws -r ./restore
```

不要让这个 alias 继承 `RemoteCommand` 或 `RequestTTY`。`hgc` 不负责安装 WSL sshd、配置 Windows 网络或编辑 SSH config。在原生 Windows 上，`\\wsl.localhost\DISTRO\home\...` 可作为本地 `--root`，但这不是上述远端 WSL 传输方案，而且跨 Windows/WSL 文件系统访问可能有性能成本（[Microsoft WSL filesystem guidance](https://learn.microsoft.com/en-us/windows/wsl/filesystems)）。

### 项目构建产物清理

`hgc file purge` 会查找可重建的项目产物，例如依赖目录、构建输出、测试与工具缓存，以及带有有效 `CACHEDIR.TAG` 的目录。先用预览模式检查候选项：

```sh
hgc file purge --dry-run
hgc file purge
hgc file purge ~/Work/client-a ~/scratch/project-b
hgc file purge --paths
```

位置参数 `PATH...` 只作为本次调用的临时扫描根目录，并会取代已保存的路径列表和自动发现结果。未传位置参数时，非空的用户路径文件会取代自动发现；文件缺失或只包含空白和注释时，命令恢复使用自动根目录。单独运行 `--paths` 可创建或编辑该文件，显示已配置根目录的存在状态，并在编辑器退出后重新显示生效列表。编辑器按 `$VISUAL`、`$EDITOR`、Windows 上的 `notepad.exe`、macOS 上的 `open -W -t` 或其他平台上的 `vi` 依次选择。空行和 `#` 注释会被忽略，每行必须是绝对路径或 `~` 路径。

路径文件在 Linux 和 WSL 上位于 `${XDG_CONFIG_HOME:-~/.config}/hagency/space-purge-paths`，在 macOS 上位于 `~/Library/Application Support/Hagency/space-purge-paths`，在 Windows 上位于 `%APPDATA%\Hagency\space-purge-paths`。`APPDATA` 不可用时，Windows 回退到 `~/.config/hagency/space-purge-paths`。

自动发现会检查已存在的 `~/www`、`~/dev`、`~/Projects`、`~/GitHub`、`~/Code`、`~/Workspace`、`~/Repos`、`~/Development`、`~/.codex/worktrees` 和 `~/.claude/worktrees`，也会扫描主目录的直接子目录，只纳入两层深度内存在项目标记的容器。系统目录和云存储根目录不会被自动发现。

交互式 TTY 中，Questionary 会显示多选列表。产物目录及其后代文件的最后活动时间严格超过 7 天时，候选项默认选中；较新或无法确定时间的候选项保留在列表中，但不会预选。常规清理会输出每个已选项的绝对路径，再请求二次确认，且默认回答为否。使用 `--dry-run` 时仍会打开多选列表，但只预览已选项，并跳过破坏性的二次确认。非 TTY 环境会跳过交互界面，预览所有候选项及其默认选中状态，即使没有传入 `--dry-run` 也不会删除文件。

该命令会永久删除已选产物，不会将它们移到废纸篓或回收站。Git 已跟踪的候选项（包括内部嵌套 Git 仓库含有 tracked 文件时的整个候选项）、符号链接、junction、reparse path、挂载点、大小为零的候选项、Xcode 的全局 `DerivedData` 目录、非 Composer 项目中的 `vendor` 目录，以及非 `.NET` 项目中的 `bin` 目录都会被排除。通用产物名只在有项目标记支持时才会成为候选项。如果永久删除因权限、文件系统变化或其他 I/O 错误而中途失败，部分内容可能已经被删除；命令会报告失败、继续处理后续选项，并以状态码 1 退出。

### 本地模型代理

`hgc service model-proxy start` 会启动后台进程，并为每个 provider 同时提供 OpenAI Responses 和 Chat Completions 接口。Provider 只由 URL 选择，绝不根据 `model` 路由；`model` 值原样发给上游。

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
hgc service model-proxy start -r <workspace>
hgc service model-proxy stop -r <workspace>
hgc service model-proxy restart -r <workspace>
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

`skill add` 从上述输入中选择并安装技能。默认链接到调用目录的 `.agents/skills`。`-p/--path` 直接指定最终 skills 容器，命令只会在其后追加 skill 名；`-d/--dir` 指定 workspace 根目录，安装目标为 `<workspace>/.agents/skills`；`--global` 安装到 `~/.agents/skills`。这三个选项互斥。相对目标路径以调用目录为基准，`~` 展开为当前用户的主目录。`--root` 和 `--checkout-dir` 用于解析来源，不会改变安装目标；快捷安装首次登记远程来源时，会将 checkout override 保存为实际解析后的路径。非 Windows 平台使用 symlink，Windows 使用 junction。

`profile apply` 必须且只能选择一个目标：`-p/--path` 是最终 skills 容器，`-d/--dir` 是 workspace 根目录，对应目标为 `<workspace>/.agents/skills`。目标路径同样以调用目录为基准并支持 `~` 展开，`--root` 和 `--checkout-dir` 仍只用于 discovery。传入 workspace 根目录时应使用 `--dir`。应用 profile 不会清理未选中的旧安装，也不会覆盖作为独立副本的真实目录；指向其他来源的既有 symlink/junction 可以被重新指向。本次没有新增安装记录或 prune 行为。

如果发现多个安装名称相同的 skill 目录，交互式终端会要求为本次应用选择一个来源路径，且不会改写 profile。非交互安装会失败，并提示改在终端中运行或收窄 profile 的 `include` selector；使用 `--dry-run` 时，无论是否为 TTY，都会列出每个冲突来源并继续预览，不弹出选择提示。

## Skills

| Skill | 适用场景 | 作用 |
| --- | --- | --- |
| [`analyze-diff`](skills/analyze-diff/SKILL.md) | 解释 git diff、提交范围、分支对比或粘贴的变更集 | 把原始变更证据整理成面向发布的摘要、功能变更列表、风险说明、测试缺口和发布说明草稿。 |
| [`diagnose-ai-workflow`](skills/diagnose-ai-workflow/SKILL.md) | 审计 prompt、Agent 工作流、工具链、多 Agent 系统或生产就绪度 | 基于现有证据，从 prompt、上下文、工具、架构、安全、可靠性和系统性能等维度评估工作流健康度。 |
| [`hagency-cli`](skills/hagency-cli/SKILL.md) | 使用 Hagency Kit CLI 管理 source、profile、skill、在线或离线项目文件同步、项目构建产物清理、profile 应用或本地模型代理 | 帮助 Agent 管理 Hagency workspace 内容、同步项目文件、安全预览项目构建产物清理，并运行 provider 级 Responses/Chat 代理接口。 |
| [`log-analyzer`](skills/log-analyzer/SKILL.md) | 调查应用、服务器、JSON、CI 或轮转 gzip 日志 | 通过采样和分析日志解释故障、错误峰值、慢请求、流量模式和事故信号，同时控制证据范围并做脱敏处理。 |

## Profiles

Profile 是用于 Agent 工作流场景的轻量级捆绑定义。

Profile 在 `profiles/<name>/config.toml` 中声明要启用的 source 名和 skill selector。应用 profile 后，选中的 skills 会被物化到指定的 skills 容器；`-d/--dir` 使用 workspace 下的 `.agents/skills` 容器。
