# notion-local-ops-mcp

[English](./README.md)

把 Notion 里的 **MCP Agent** 变成一个可操作本地文件、shell、git 和委托任务的 coding agent。

![在本地仓库中工作的 MCP Agent](./assets/notion/notion-handoff-chat.png)

## 这个项目做什么

- 通过 MCP 暴露本地文件、shell、git 和 patch 式编辑能力
- 让 MCP Agent 真正在本地仓库里工作，而不只是编辑 Notion 页面
- 支持通过本地 `codex` 或 `claude` 委托长任务

## 快速开始

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp
cp .env.example .env
./scripts/dev-tunnel.sh
```

至少设置：

```bash
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

## 关键 MCP Agent 配置

在 Notion 里的 MCP Agent 配置中使用：

- URL：`https://<your-domain-or-tunnel>/mcp`
- Auth type：`Bearer`
- Token：`NOTION_LOCAL_OPS_AUTH_TOKEN`

下面这版 prompt 是给 **MCP Agent** 用的，不是给 Notion AI 指令页用的。

<details>
<summary><strong>推荐 MCP Agent prompt</strong></summary>

```text
You are a pragmatic local operations agent connected to my computer through MCP.

Goals:
- Complete file, code, shell, and task workflows end-to-end with minimal interruption.
- Act more like a coding agent than a chat assistant.
- Stay concise, direct, and outcome-focused.

Disambiguation rules:
- If the context contains local repo paths, filenames, code extensions, README, AGENTS.md, CLAUDE.md, or .cursorrules, treat "document", "file", "notes", "instructions", and "docs" as local files unless the user explicitly says Notion page, wiki, or workspace page.
- If the user asks to edit AGENTS.md, CLAUDE.md, README, or project instructions inside the repo, edit the local file. Do not switch into self-configuration or setup behavior unless the user explicitly says to change the agent itself.
- For local file edits, do not use <edit_reference>. That is for Notion page editing, not MCP file changes.
- When answering code questions, prefer file paths, line references, function names, command output, or git diff over Notion-style citation footnotes.

Working style:
- First restate the goal in one sentence.
- Default to the current workspace root unless the target path is genuinely ambiguous.
- For non-trivial tasks, give a short plan and keep progress updated.
- Prefer direct tools first. Use delegate_task only when direct tools are not enough.
- Keep moving forward instead of asking for information that can be discovered via tools.
- If the user says fix, change, implement, deploy, update, or similar imperative requests, execute directly instead of stopping after analysis.
- If information is missing, probe with tools first. Use ask-survey only when tool probing still cannot resolve a decision and the next step is destructive or high-risk.

Tool strategy:
- list_skills: use when the user asks what skills are available in this repo or globally.
- server_info: call first when troubleshooting connection/runtime mismatches.
- Pass explicit cwd for every project operation, including files/search/patch/skills/Shell/Git. set_default_cwd / get_default_cwd are legacy PROCESS-WIDE state shared by all clients; file tools without cwd still use WORKSPACE_ROOT.
- In coding tasks, search the local repo first. Do not default to searching the Notion workspace.
- apply_patch: 现有文件默认优先用它编辑，包括小改动、多 hunk 改动、移动、删除或一次 patch 里新增文件。每个 @@ hunk 都必须至少包含一行 '+' 或 '-'，并且必须在文件里唯一匹配。需要预检或预览时再用 dry_run=true / validate_only=true / return_diff=true。
- write_file: create new files or rewrite short files when that is simpler than patching; use dry_run=true for no-write preview.
- run_command_stream: start long-running shell jobs with immediate task_id return for polling progress. Prefer it for tests, installs, builds, compile steps, and other jobs that may take a while.
- get_task / wait_task: check delegated task or background command status; prefer wait_task when blocking is useful.
- run_command: proactively use for short non-destructive commands such as pwd, ls, rg, or small smoke checks.
- search: canonical query tool. mode='glob' for path discovery, mode='regex' for regex/code search, mode='text' for literal substring search. Hidden entries and .gitignore'd paths are excluded by default; regex/text search can target a single file path directly.
- list_files: inspect directory structure only when structure matters; paginate with limit and offset when needed.
- read_text: canonical single/batch file reader with line-based pagination; set include_line_numbers=true when the result will be cited or reviewed line-by-line.
- git_status / git_diff / git_commit / git_log / git_show / git_blame: use these as the default repository workflow and traceability tools only when the current cwd is actually inside a git repo.
- delegate_task: use only for complex multi-file reasoning, long-running fallback execution, or repeated failed attempts with direct tools by local codex or claude-code. For non-trivial work, pass goal, acceptance_criteria, verification_commands, and commit_mode.
- cancel_task: stop a delegated task if needed.
- purge_tasks: garbage-collect stale task artifacts under STATE_DIR/tasks (dry_run first).

Execution rules:
- When exploring a codebase, prefer search(mode='glob' or 'regex') over broad list_files calls.
- Follow the loop: probe, edit, verify, summarize.
- Do the minimum necessary read/explore work before editing.
- Before editing, inspect the project baseline and preserve unrelated changes. After editing, re-read changed sections, inspect the diff, and run relevant verification.
- Prefer apply_patch for edits to existing files; reserve write_file for new files or full rewrites.
- Do not issue parallel writes to the same file.
- After a logically meaningful change, inspect git_status and git_diff. Commit only when the user or applicable repository rules authorize it; do not automatically commit during acceptance testing.
- Use focused commits. Do not mix unrelated changes in one commit.
- Use clear commit messages, preferably conventional commit style such as fix, feat, docs, test, refactor, or chore.
- For destructive actions such as deleting files, resetting changes, or dangerous shell commands, ask first.
- If a command or delegated task fails, summarize the root cause and adjust the approach instead of retrying blindly.

Reliability rules:
- Follow next_cursor and deferred reads; partial content is not the whole file. On file_changed, re-read before continuing.
- On wait_timed_out, keep the same task_id and continue querying; never restart the command just because waiting timed out.
- If a submission response is lost, the outcome is unknown. Do not blindly resubmit side-effecting commands; first reconcile available records or ask for help.
- On capacity_exceeded, retry later without busy polling. On partial write failure, re-read affected files and inspect the diff before repair; do not replay the entire patch.
- cancel_requested is not confirmed termination. On cleanup_failed or control_unavailable, report uncertainty and stop steps that depend on the process having exited.
- Even after completed=true, continue reading while has_more=true. Disclose output truncation; never treat missing logs as proof of success.
- Report completion only when the requested change and its verification both succeed. Do not repeat unchanged failing commands. First-stage Notion acceptance uses direct tools, not delegate_task.

Verification rules:
- After code changes, prefer this minimum verification ladder when applicable:
- 1. Syntax or compile check such as cargo check, tsc --noEmit, python -m py_compile, or equivalent.
- 2. Focused tests for the changed area, or the nearest relevant test target.
- 3. Smoke test for the changed behavior, such as starting a service or running curl against the affected endpoint.
- Do not skip verification unless the user explicitly says not to run it.

Output style:
- Before tool use, briefly say what you are about to do.
- During longer tasks, send short progress updates.
- At the end, summarize result, verification, and any remaining risk or next step.
```

</details>

## Relay 桥接（把工具调用镜像到 dashboard）

和 [workspace-agent-relay-mcp](https://github.com/catoncat/workspace-agent-relay-mcp) 配合时，agent 在这里每调一次工具，都会自动镜像到那个 relay 的实时 dashboard——本地操作者能看着 agent 干活，agent 完全不用手动汇报。

工作原理：

1. agent 在这里调 `bind_relay_run`，传入 relay trigger 给的 `request_id` + `callback_token`（不用传 relay 地址，本地已配好）。
2. 之后每个 `@traced` 工具调用都会异步发一个 `POST /internal/tool-trace` 给 relay。
3. relay 存下来并通过 SSE 推到 dashboard。

这是一个**通用 instrumentation 层**（`@traced` 装饰器 + 可插拔 sink），relay 只是其中一个 sink，并非硬编码。relay 不可达绝不阻塞工具执行——trace 是尽力而为。

环境变量：

```bash
NOTION_LOCAL_OPS_RELAY_BRIDGE_ENABLED=true        # 默认 true；设 false 完全关闭
NOTION_LOCAL_OPS_RELAY_URL=http://127.0.0.1:8799  # bind_relay_run 默认用的 relay 地址
NOTION_LOCAL_OPS_RELAY_BRIDGE_TIMEOUT=1.5         # 秒，trace POST 的超时上限
```

`bind_relay_run` / `clear_relay_run` 是给 agent 用的控制工具；`server_info` 暴露 `relay_bridge` 状态（enabled、bound、dropped_traces 等）。关闭或未绑定时，行为零变化。

## 可选应用场景

如果你还想使用 **Notion AI 页面级指令 + 项目管理** 这套工作流，见：

- [Optional use case: Notion AI instruction page + project management](./docs/notion-use-case.md)
- [可选应用场景：Notion AI 页面级指令 + 项目管理](./docs/notion-use-case.zh-CN.md)

## 运行要求

- Python 3.11+
- FastMCP 3.x（`fastmcp>=3.2.4,<4`，由 `pyproject.toml` 安装）
- `cloudflared`
- 一个可在 Notion 中配置自定义 MCP 的 **MCP Agent**
- 可选：`codex` CLI
- 可选：`claude` CLI

## 详细配置

如果你想按完整步骤配置，可以走这条路径：

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

cp .env.example .env
```

编辑 `.env`，至少设置：

```bash
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

然后运行：

```bash
./scripts/dev-tunnel.sh
```

你应该看到：

- 脚本创建或复用 `.venv`
- 自动安装缺失或版本不匹配的 Python 依赖
- 本地 MCP 服务通过一个平滑重载 supervisor 启动在 `http://127.0.0.1:8766/mcp`
- 脚本会打印 `./scripts/dev-tunnel.sh reload`，用于不掉 tunnel 地重载本地服务
- 优先使用 `cloudflared.local.yml` 命名 tunnel
- 否则回退到 `cloudflared` quick tunnel，并打印公网 HTTPS 地址

在 Notion 里配置时，使用这个输出地址并在后面补上 `/mcp`，同时使用 `NOTION_LOCAL_OPS_AUTH_TOKEN` 作为 Bearer token。

### 手动安装

```bash
git clone https://github.com/<your-account>/notion-local-ops-mcp.git
cd notion-local-ops-mcp

python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 配置

如果你不使用一键启动流程，就先复制 `.env.example` 到 `.env`，至少设置：

```bash
cp .env.example .env
NOTION_LOCAL_OPS_WORKSPACE_ROOT="/absolute/path/to/workspace"
NOTION_LOCAL_OPS_AUTH_TOKEN="replace-me"
```

可选项：

```bash
NOTION_LOCAL_OPS_CODEX_COMMAND="codex"
NOTION_LOCAL_OPS_CLAUDE_COMMAND="claude"
NOTION_LOCAL_OPS_COMMAND_TIMEOUT="120"
NOTION_LOCAL_OPS_DELEGATE_TIMEOUT="1800"
NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS="30"
NOTION_LOCAL_OPS_LAUNCHD_LABEL_PREFIX="com.notion-local-ops"
```

### 手动启动

```bash
source .venv/bin/activate
notion-local-ops-mcp
```

本地地址：

```text
http://127.0.0.1:8766/mcp
```

### 一键本地开发 Tunnel

推荐的本地工作流：

```bash
./scripts/dev-tunnel.sh
```

这个脚本会：

- 复用或创建 `.venv`
- 安装缺失的运行时依赖
- 如果存在 `.env`，自动从仓库根目录加载
- 在平滑重载 supervisor 后面启动 `notion-local-ops-mcp`
- 通过 `./scripts/dev-tunnel.sh reload` 先拉起新进程、再排空旧进程，尽量避免 tunnel 短暂 502
- 如果存在 `cloudflared.local.yml` 或 `cloudflared.local.yaml`，优先使用它
- 否则自动打开一个 `cloudflared` quick tunnel

注意：

- `.env` 已加入 gitignore，所以本地 token 和 workspace 路径不会进 git
- `cloudflared.local.yml` 已加入 gitignore，所以你的本地 tunnel 配置也不会进 git
- 如果 `NOTION_LOCAL_OPS_WORKSPACE_ROOT` 未设置，脚本会默认使用仓库根目录
- 如果 `NOTION_LOCAL_OPS_AUTH_TOKEN` 未设置，脚本会直接报错退出，而不是猜测
- `./scripts/dev-tunnel.sh reload` 会向 supervisor 发送 `SIGHUP`，在不丢公网 `/mcp` 入口的情况下滚动替换本地服务进程
- 全新 clone 后，通常不需要先手动执行 `pip install`

### 不掉 Tunnel 的平滑重载

当 `./scripts/dev-tunnel.sh` 已经在一个终端或 tmux pane 里跑起来后，可以在另一个 shell 执行：

```bash
./scripts/dev-tunnel.sh reload
```

这个命令会保持 `cloudflared` 仍然连在同一个本地端口上，同时 supervisor 先拉起新的 MCP 服务、确认 ready，再让旧进程进入 drain。调试代码时，推荐优先用它而不是直接把整条 tunnel 会话杀掉。

### macOS 持久化 launchd 安装

如果你的目标是“关掉 shell / tmux 之后也要继续跑”，就安装 launchd 版本：

```bash
./scripts/install-launchd.sh
```

它会安装：

- 一个本地 MCP supervisor 的 LaunchAgent
- 一个 `cloudflared tunnel run` 的 LaunchAgent
- 一个 timer 型 LaunchAgent，每隔 `NOTION_LOCAL_OPS_WATCHDOG_INTERVAL_SECONDS`
  秒执行一次 `launchd-doctor.sh --fix`
- 两者退出后由 `launchd KeepAlive` 自动拉起
- local `/mcp` 或 public `/mcp` 连续
  `NOTION_LOCAL_OPS_DOCTOR_FAILURE_THRESHOLD` 次检查失败时，只重启对应失败层
- 重启使用指数退避：从 `NOTION_LOCAL_OPS_DOCTOR_BASE_BACKOFF_SECONDS`
  开始，最大不超过 `NOTION_LOCAL_OPS_DOCTOR_MAX_BACKOFF_SECONDS`

安装后常用命令：

```bash
./scripts/launchd-status.sh
./scripts/launchd-doctor.sh         # 判断 local / public 哪层挂了
./scripts/launchd-doctor.sh --fix   # 只重启失败层
./scripts/launchd-reload.sh          # 代码更新后的平滑 reload
./scripts/launchd-restart.sh mcp     # 依赖/环境变更后的 MCP 全量重启
./scripts/launchd-restart.sh all     # MCP + cloudflared 一起重启
./scripts/uninstall-launchd.sh
```

更新代码时的建议：

- 仅 Python / 代码更新：`./scripts/launchd-reload.sh`
- 依赖 / `.venv` / env 变更：如果依赖约束或 plist 环境可能过期，先重跑
  `./scripts/install-launchd.sh`；否则用 `./scripts/launchd-restart.sh mcp`
- tunnel 配置变更：`./scripts/launchd-restart.sh cloudflared`
- watchdog 间隔变更：设置 `NOTION_LOCAL_OPS_WATCHDOG_INTERVAL_SECONDS` 后重跑
  `./scripts/install-launchd.sh`
- doctor / 退避参数变更：设置 `NOTION_LOCAL_OPS_DOCTOR_FAILURE_THRESHOLD`、
  `NOTION_LOCAL_OPS_DOCTOR_BASE_BACKOFF_SECONDS` 或
  `NOTION_LOCAL_OPS_DOCTOR_MAX_BACKOFF_SECONDS` 后重跑
  `./scripts/install-launchd.sh`

### 用 cloudflared 暴露服务

#### Quick tunnel

```bash
cloudflared tunnel --url http://127.0.0.1:8766
```

使用生成的 HTTPS 地址，并在后面补 `/mcp`。

#### Named tunnel

把 [`cloudflared-example.yml`](./cloudflared-example.yml) 复制成 `cloudflared.local.yml`，填入你的真实值，然后运行：

```bash
cp cloudflared-example.yml cloudflared.local.yml
./scripts/dev-tunnel.sh
```

或者手动运行 cloudflared：

```bash
cloudflared tunnel --config ./cloudflared-example.yml run <your-tunnel-name>
```

## 环境变量

| 变量 | 必填 | 默认值 |
| --- | --- | --- |
| `NOTION_LOCAL_OPS_HOST` | 否 | `127.0.0.1` |
| `NOTION_LOCAL_OPS_PORT` | 否 | `8766` |
| `NOTION_LOCAL_OPS_WORKSPACE_ROOT` | 是 | home directory |
| `NOTION_LOCAL_OPS_STATE_DIR` | 否 | `~/.notion-local-ops-mcp` |
| `NOTION_LOCAL_OPS_AUTH_TOKEN` | 否 | empty |
| `NOTION_LOCAL_OPS_CLOUDFLARED_CONFIG` | 否 | empty |
| `NOTION_LOCAL_OPS_TUNNEL_NAME` | 否 | empty |
| `NOTION_LOCAL_OPS_CODEX_COMMAND` | 否 | `codex` |
| `NOTION_LOCAL_OPS_CLAUDE_COMMAND` | 否 | `claude` |
| `NOTION_LOCAL_OPS_COMMAND_TIMEOUT` | 否 | `120` |
| `NOTION_LOCAL_OPS_DELEGATE_TIMEOUT` | 否 | `1800` |
| `NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING` | 否 | `0` |
| `NOTION_LOCAL_OPS_GRACEFUL_SHUTDOWN_SECONDS` | 否 | `30` |
| `NOTION_LOCAL_OPS_LAUNCHD_LABEL_PREFIX` | 否 | `com.notion-local-ops` |

## MCP 工具

- `list_files`：列出文件和目录并支持分页；默认排除隐藏/噪声目录并尊重 `.gitignore`
- `list_skills`：发现项目级和全局 skills，并返回名称与简介
- `search`：统一查询入口（glob 路径搜索 / regex 搜索 / literal 子串搜索）；默认排除隐藏项和 `.gitignore` 命中的路径，并支持对单文件直接做 regex/text 搜索
- `read_text`：统一单文件/批量读取入口，支持按行分页（`start_line`/`line_limit`）、可选 `include_line_numbers` 和 `language` 提示
- `write_file`：整文件写入，支持 `dry_run`
- `apply_patch`：现有文件的默认编辑工具；使用 `*** Begin Patch` / `*** Update File` 文本语法，拒绝纯 context hunk，要求唯一 context 匹配，并返回每文件改动统计/警告
- `server_info`：查看运行时配置与已注册工具清单
- `set_default_cwd`：设置进程级共享的 Shell/Git 默认目录；多对话请显式传 `cwd`
- `get_default_cwd`：查看当前会话/生效工作目录
- `git_status`：结构化仓库状态（仅在 cwd 位于 git 仓库内时使用）
- `git_diff`：按文件分组的结构化 diff（含每文件独立截断）
- `git_commit`：stage 指定路径或全部改动后创建 commit（支持 `amend` / `allow_empty` / `author` / `sign_off` / `dry_run`）
- `git_log`：最近提交历史
- `git_show`：查看指定 commit/ref 的元信息与逐文件 diff
- `git_blame`：查看文件（可选行区间）的逐行 blame 元数据
- `run_command`：运行本地 shell 命令，支持后台模式
- `run_command_stream`：启动后台 shell 任务并通过 task 轮询进度；长测试 / build / install / compile 优先走它
- `delegate_task`：把任务交给本地 `codex` 或 `claude-code`，支持 `goal`、`acceptance_criteria`、`verification_commands`、`commit_mode`
- `get_task`：读取后台任务状态和输出尾部
- `wait_task`：阻塞等待后台 shell 任务或委托任务完成或超时
- `cancel_task`：停止后台 shell 任务或委托任务
- `purge_tasks`：清理 `STATE_DIR/tasks` 下的旧任务产物（支持 `dry_run`）

Relay 桥接控制工具（见 [Relay 桥接](#relay-桥接把工具调用镜像到-dashboard)）：

- `bind_relay_run`：把当前进程绑定到一个 relay run，之后工具调用自动镜像
- `clear_relay_run`：解绑；等价于 `bind_relay_run` 传空 `request_id`

## 调试 Notion / MCP 握手卡住

如果客户端显示已连接但卡在 initialize、tools/list 或 tool call，可开启 MCP 详细日志：

```bash
NOTION_LOCAL_OPS_DEBUG_MCP_LOGGING=1 ./scripts/dev-tunnel.sh
```

开启后，server log 会输出 `MCP_DEBUG` 行，包含：

- HTTP method / path
- session id 提示
- JSON-RPC method
- `tools/call` 的 tool 名
- `tools/call.arguments` 的截断摘要
- 响应状态码与耗时

## 验证

### 第一阶段：可靠的本地 coding 工具

每个项目操作都显式传 `cwd`。相对 `cwd` 以 `WORKSPACE_ROOT` 为基准；相对文件路径以本次 `cwd` 为基准；绝对文件路径保持其目标。省略 `cwd` 的旧文件调用仍使用 `WORKSPACE_ROOT`，旧 Shell/Git 调用仍使用进程级默认目录。全局默认目录和 relay 绑定尚未按对话隔离。

以下是工具调用的 JSON 参数示例，替换项目路径和服务返回的 ID/cursor 后使用：

`read_text`：

```json
{"cwd":"/absolute/path/to/project","path":"src/app.py","start_line":1,"line_limit":200}
```

续读优先使用返回的 `next_cursor`，不再同时传 `start_line`：

```json
{"cwd":"/absolute/path/to/project","path":"src/app.py","cursor":"<next_cursor>"}
```

`partial_line=true` 表示超长行尚未读完，此时 `next_offset` 为空，必须使用 cursor。文件变化返回 `file_changed`，需要重新读取。文本统一换行为 LF，展示内容省略末尾换行；精确拼接分页文本时使用 `content + separator_after`（关闭行号）。无效 UTF-8 被拒绝。批量读取中的 `deferred` 文件未被读取，可按返回路径和 `next_start_line` 单独继续。

`write_file` / `apply_patch` 同样接受 `cwd`：

```json
{"cwd":"/absolute/path/to/project","path":"notes.txt","content":"verified\n","dry_run":true}
```

`run_command_stream`：

```json
{"cwd":"/absolute/path/to/project","command":"python3 -u -c \"import time; print('STARTED'); time.sleep(3); print('DONE')\"","timeout":20}
```

用 `wait_task` 读取中间输出；`cursor="start"` 从已保存日志的开头开始，后续换成返回的 `next_cursor`：

```json
{"task_id":"<task_id>","cursor":"start","return_on_output":true,"timeout":10,"max_bytes":32768}
```

`get_task` 接受相同的 `cursor` / `max_bytes`，不等待。省略 cursor 保留旧的输出尾部查询。stdout/stderr 分别保证顺序，不承诺两者之间的精确时序。`has_more` 表示还有已保存的日志，任务 `completed=true` 后也可能需要继续读取。

`wait_timed_out` 只说明这次等待结束，不取消任务；`execution_timed_out` 才表示命令超过执行时限。旧 `timed_out` 在 `wait_task` 中仍表示等待超时，在 `get_task` 中保留执行超时含义；新调用请使用两个明确字段。单次等待最大 30 秒，长任务需要分次等待。

`cancel_task` 参数为 `{"task_id":"<task_id>"}`。收到 `cancel_requested` 不代表已停止；确认 `cancelled=true` 和 `completed=true`。取消会处理同一受管理进程组，先 TERM 再 KILL；已完成任务保持原结果。无法确认清理时返回 `cleanup_failed`，任务仍占用执行额度；旧服务进程的活动记录返回 `control_unavailable`。本阶段不接管跨重启任务，不支持交互输入/PTY或自行脱离进程组的后台服务；非 POSIX 平台明确拒绝受管理执行。

默认上限：单次文件文本 32 KiB、最多 2,000 行，批量最多 16 文件/64 KiB；任务查询 32 KiB、每任务保存 16 MiB 日志；同一进程最多 4 个执行。配置见 `.env.example`，实际值由 `server_info.limits` 返回。日志达到保留上限后继续消费管道并丢弃后续超额文本，`output_truncated` / `dropped_bytes` 会明确报告；不要据此声称日志完整。同步命令输出也有 32 KiB 上限。任务提交元数据和同步命令限制为 64 KiB，较大的上下文请放入文件。

清理仅删除超过保留时间的终态任务；活跃、损坏和链接目录会跳过。文件/多文件补丁不是事务：失败可能已产生部分修改，请检查 `applied_files`、`failed_file`、`not_applied_files`、`requires_reread` 等字段并重新读取，不能直接整批重试。

实现计划和分层验收见 [三阶段计划](docs/notion-code-agent-plan.zh-CN.md) 与 [第一阶段验收记录](docs/phase1-verification.zh-CN.md)。

```bash
source .venv/bin/activate
pytest -q
python -m compileall src tests
```

### 本地 MCP 调用模拟测试

下面这组用例会本地模拟真实 MCP client/server 流程（initialize + call_tool + wait_task）：

```bash
source .venv/bin/activate
pytest -q tests/test_server_transport.py tests/test_concurrent_clients.py tests/test_mcp_local_simulation.py
```

## 故障排查

### Notion 提示无法连接

- 确认 URL 以 `/mcp` 结尾
- 确认鉴权类型是 `Bearer`
- 确认 token 与 `NOTION_LOCAL_OPS_AUTH_TOKEN` 一致
- 确认 `cloudflared` 仍在运行
- 如果你已经安装了 macOS LaunchAgent，先跑 `./scripts/launchd-status.sh`
- 如果你在用户连接期间需要更新服务，优先使用 `./scripts/dev-tunnel.sh reload` 或 `./scripts/launchd-reload.sh`，不要直接把整条 tunnel 会话杀掉

### 本地 `/mcp` 正常，但通过 tunnel 不通

- 优先改用 named tunnel 再试
- 用真实 MCP client 验证 `/mcp`，例如：

```bash
source .venv/bin/activate
fastmcp list http://127.0.0.1:8766/mcp
```

### 重启过程中 Notion 短暂看到 502

- 重启时出现 Cloudflare 502，通常表示源站短暂不可用，不是 Cloudflare 在拦截
- 如果这是在你手动杀 tmux pane 时发生的，改用 `./scripts/dev-tunnel.sh reload`，让 supervisor 以滚动替换方式重载服务
- 查看最新的 `notion-local-ops-mcp-server.*.log`，确认新进程 ready 之后旧进程才开始 drain

### 日志里反复出现 404

- 如果 404 是 `GET /`，通常是配置 URL 时漏掉了结尾的 `/mcp`
- 如果已经是 `/mcp` 仍出现 404/405，请升级到把 `/mcp` 改为 streamable HTTP 的版本

### `delegate_task` 失败

- 检查 `codex --help`
- 检查 `claude --help`
- 必要时设置 `NOTION_LOCAL_OPS_CODEX_COMMAND` 或 `NOTION_LOCAL_OPS_CLAUDE_COMMAND`
