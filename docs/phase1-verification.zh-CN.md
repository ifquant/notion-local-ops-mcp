# 第一阶段实现与验收记录

日期：2026-10-15

状态：1A–1E 已实现，A1–A13 本地验收及 G0/A14 真实 Notion 验收通过。第一阶段完成范围为同一存活服务进程内、显式目录、非交互任务；临时测试部署不等于长期常驻或跨重启接管。

## 已实现的行为

- 文件、搜索、补丁和项目 skills 支持显式 `cwd`，保留省略参数时的旧路径行为。一次调用固定一个路径基准，补丁移动目标使用同一基准。
- 文本使用有界流式读取，提供文件版本绑定的续读 cursor，支持超长行和批量预算/deferred，避免字节截断导致漏读。
- 后台 Shell 与委派任务共用受管理进程执行，执行中持续保存输出；查询使用有界读取和输出游标。
- 区分等待超时与执行超时；默认等待终态，可选择有新输出即返回；完成后仍可读取剩余日志。
- POSIX 进程组 TERM/KILL、终态不可覆盖、未知控制权不伪报取消；清理无法确认时保留非终态与执行额度，并由原 worker 继续核对。
- 同步命令复用进程管理并与后台任务共享并发额度。输出、日志保留、输入元数据、批量读取和等待均有限制。
- 清理保护活跃/损坏/符号链接任务；读取与清理串行协调。文件或多文件补丁写入失败会如实报告可能已发生的修改。
- README 中更新调用示例、兼容性与能力边界；`.env.example` 和 `server_info.limits` 提供资源限制说明。
- 服务握手指令和中英文 README 的 Agent 提示词补齐续读、回读/diff 核对、等待与执行区分、未知提交结果、部分写入和取消失败的处理规则；验收过程不自动提交代码。
- 实际部署发现 `dev-tunnel.sh` 未加载 `ensure_python_runtime_deps`；补充引用现有 `launchd-common.sh` 后，启动脚本成功拉起 supervisor、服务和隧道，未另造部署框架。

## 验证环境与命令

早期测试使用 uv 隔离依赖环境。经用户确认授权后，已在仓库创建 `.venv` 并安装 editable 项目，真实接入与最近两次全量回归使用该虚拟环境；没有更改全局 Python 包或安装/重启 launchd 服务。

| 项目 | 版本 |
| --- | --- |
| 系统 | macOS 26.3.1，arm64 |
| Python | 3.11.4 |
| FastMCP | 3.4.8 |
| MCP Python SDK | 1.30.0 |
| uvicorn | 0.54.0 |
| pytest | 9.1.1 |
| httpx / Starlette | 0.28.1 / 1.8.0 |

在仓库根目录执行：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy -u HTTPS_PROXY -u https_proxy \
  .venv/bin/python -m pytest -q -p no:cacheprovider --tb=short

python3.11 -m compileall -q src tests
git diff --check
```

取消代理变量只影响测试命令进程，以免本机 SOCKS 代理依赖影响 localhost 客户端。部署使用 `cloudflared 2026.9.3`，配置在被 Git 忽略且权限为 0600 的 `.env`；令牌不记录在此文档或代码快照中。

最近一次全量回归结果：**221 passed，1 warning，27.36 秒**。警告是既有测试使用 httpx 与 Starlette TestClient 的弃用提示。语法编译、启动脚本 `bash -n` 和已跟踪文件空白检查通过；新增文件另做 no-index 空白检查。

前一次全量回归为 220 passed / 1 failed：`test_supervisor_reload_keeps_mcp_endpoint_available` 在切换期间出现一次 `ReadError`。随后单项复测通过（2.91 秒），再全量通过；根因未修复，不据此宣称重载零断线。第一阶段的验收保证限定在同一存活服务进程内。

## 对照计划的验收证据

以下测试位于 `tests/`，均包含在上述全量结果中。测试命令使用本地假委派执行器，不启动真实模型推理。

| 项目 | 主要证据 | 结果 |
| --- | --- | --- |
| A1 路径一致性与兼容 | `test_phase1_files.py::test_explicit_file_cwd_does_not_follow_shared_override`；既有 session、Git、skills 与文件测试 | 通过 |
| A2 完整分页与文件变化 | `test_cursor_roundtrip_without_skipping_long_lines`（含空文件、中文、emoji、长行、换行参数化）；`test_cursor_version_path_and_arguments`；`test_read_is_streamed_and_encoding_errors_are_explicit` | 通过 |
| A3 批量预算 | `test_batch_budget_missing_file_and_deferred`；`test_cursor_start_conflict_and_batch_count_limit` | 通过 |
| A4 实时与双流输出 | `test_live_output_cursor_utf8_and_multiple_readers`；`test_dual_stream_flood_is_bounded_and_drained`；`test_split_utf8_output_decoder` | 通过 |
| A5 游标与唤醒 | `test_completed_can_have_more_unread_output`；`test_multiple_waiters_wake_after_cancel`；`test_wrong_task_and_future_cursor_rejected`；既有 wait 事件测试 | 通过 |
| A6 超时语义 | `test_wait_timeout_never_erases_execution_timeout`；同步 shell 与 MCP 超时测试 | 通过 |
| A7 取消竞争及终态 | `test_cancel_before_invocation_prevents_launch`；`test_cancel_terminal_is_idempotent`；`test_first_stop_reason_wins_and_finished_control_rejects_stop`；`test_natural_exit_observed_before_cancel_keeps_success` | 通过 |
| A8 后代进程与清理失败 | `test_cancel_kills_term_ignoring_child_group`；`test_exited_parent_with_child_holding_pipe_is_bounded`；`test_cleanup_failure_keeps_capacity_until_confirmed_exit` | 通过 |
| A9 启动/存储异常 | `test_delegate_spawn_error_is_terminal`；`test_disk_log_failure_stops_process`；`test_metadata_failure_has_queryable_memory_error`；真实字节输出解码测试 | 通过 |
| A10 容量与日志限制 | `test_capacity_shared_and_released`；`test_sync_and_background_share_process_admission`；`test_sync_output_cap_and_stdin_closed`；双流洪峰测试 | 通过 |
| A11 清理与路径边界 | `test_purge_protects_active_corrupt_and_linked_tasks`；`test_purge_waits_for_read_snapshot_without_half_deleted_logs`；非法 task ID/cursor 测试 | 通过 |
| A12 部分写入失败 | `test_partial_patch_reports_real_disk_changes`；`test_direct_write_failure_requires_reread` | 通过 |
| A13 MCP 协议链路 | `test_mcp_local_simulation.py::test_phase1_project_edit_stream_and_cancel_through_mcp`；既有 transport、并发客户端和 instrumentation 测试 | 通过 |
| A14 真实 Notion | Notion 直接工具完成复现、单行修复、回读/diff、运行中日志、cursor 续读终态及重复取消；本地独立核验 | 通过 |

计划审查补充项的当前证据：

- `test_tool_write_requires_valid_authentication` 经本地 HTTP 验证无凭证/错误凭证得到 401 且文件未创建，有效凭证通过实际 MCP 会话完成写入。真实部署后另对本地与公网端点各验证缺少/错误凭证返回 401，有效凭证可发现 24 个工具并读取 `server_info`。
- MCP 会话辅助函数核对 initialize 返回的 instructions 与服务定义一致，相关协议测试已随全量回归执行；不据此推断 Notion 会遵守所有提示词。
- 新的 Agent 使用规则已加入 README 和服务指令。G0 的工作区、实际部署鉴权、Notion 连续工具调用与 10 秒分次等待已实测；Notion 在第二次请求中携带上一轮日志 cursor，成功续读原任务。

额外检查了日志查询不调用全文件读取函数，以及 JSON 的 `false` / `0` 不会因真假值判断而丢失。结构化解析结果会返回解析状态；`output_schema` 尚不执行 schema 验证，明确返回 `schema_validation="not_performed"`。

## 真实 Notion 部署与验收

用户明确确认授权部署测试服务、建立外部入口和新增 MCP 连接。2026-10-15 11:43–11:45（Asia/Shanghai），在 **if quant’s Space** 的普通 Notion Agent 完成以下验收，未更改已有 Wise Strategist Custom Agent。

- 连接名称：`Local Code Agent — Phase 1 Test`，Notion Connections 显示 `Connected 1`。
- 测试入口：Cloudflare 临时 HTTPS 隧道的 `/mcp`；具体地址保留在本地验收记录，不发布到仓库。本地只绑定 `127.0.0.1:8766`，Bearer 鉴权启用，未公开密钥。
- 专用目录：`/Users/dev/workspace2/notion-mcp-phase1-test.x1VDka`；任务状态位于其中 `.mcp-state`。目录权限 0700，但这不是进程沙箱。
- Notion 验收对话为「Phase 1 本地验收 MCP测试」，私有会话链接保留在本地 `.acceptance/phase1-verification-local.zh-CN.md`。服务日志确认客户端为 `notion-mcp-client`，实际调用了文件、Git、命令和任务工具，无 `delegate_task` 代做。

| 验收步骤 | 实际证据 | 结果 |
| --- | --- | --- |
| 搜索、读取、复现 | Notion 使用显式 cwd，复现 `test_positive` / `test_negative` 两项失败 | 通过 |
| 修改与回读 | `apply_patch` 仅将 `calc.py` 中 `a - b` 改为 `a + b`，Notion 回读并调用 git_diff | 通过 |
| 运行中读取 | 任务 `e40221b571d9`：Notion 报告 `running`、`completed=false`、`PHASE1_STARTED pid=5583` 和 next_cursor；本地核验门闩尚未存在且进程活着后才释放 | 通过 |
| 同一任务续读 | Notion 带原 cursor 继续查询，读到 `PHASE1_FINISHED exit=0`、两项测试 OK、`succeeded` / exit_code=0；没有重新提交 | 通过 |
| 取消与重复取消 | 任务 `0a8dff84c6da` 输出 `CANCEL_READY pid=6952`，忽略 TERM；Notion 两次取消均为 `cancelled` / completed=true / exit_code=-9，finished_at 不变 | 通过 |
| 独立核对 | 本地 `os.killpg(pgid, 0)` 确认进程组 5583、6952 均不存在；git diff 只有 calc.py 一行变化 | 通过 |

控制文件 `untouched.txt` 的验收前后 SHA-256 均为 `afda4801bbb6f46d1cd439310ab90bf505be0a3b84f29d9f7805a3eea0f3327d`；测试、验证脚本没有修改，无提交或推送。

## 证据保留与运行边界

- 验收代码基线为 Git `26982c862c76ec3ecaf6194bb38713403136dd05` 加当前未提交修改。专用目录 `.acceptance/` 保存 `tested-source.tar.gz`、`implementation.patch`、`notion-change.patch` 和 `mcp-server.log`；任务完整元数据和双流日志保留在 `.mcp-state/tasks/`。
- 代码快照 SHA-256：`c260ce4caf616874dc4a51ace7fe6ba2ae9a627abad70718a46749d7cf8a5284`。快照包含源码、测试、脚本、配置示例与 README，不包含 `.env`、虚拟环境或密钥。
- 当前通过 `scripts/dev-tunnel.sh` 运行，未安装 launchd。临时入口依赖当前服务和隧道进程；停机、机器休眠或重新启动隧道后可能不可用/换地址，不作为长期稳定入口。
- 服务拥有当前系统账号权限；鉴权限制调用者，不限制获授权者的文件或 Shell 范围。测试目录不是沙箱。测试期间启用了 MCP 调试日志，日志可能含调用参数，不宜用于敏感项目；后续常驻部署应关闭调试并重启生效。
- 安装 cloudflared 时 Homebrew 自动清理了旧软件版本/缓存，并自动移除 gnu-sed 4.9；已恢复安装 gnu-sed 4.10，后续安装关闭自动清理。未更改用户项目文件；旧版本/缓存的清理不能直接撤销。

可在上述 Notion 对话继续使用该测试连接。需要停止服务时，应先确认无活动任务，再终止这次 `dev-tunnel.sh` 的进程；需要撤销访问时，在 Notion Connections 断开该连接并停用测试令牌。固定域名、常驻部署和扩大到真实项目属于后续明确配置，不因本次验收而自动开启。

跨重启接管、自动再次触发分析、独立执行器和持久化工作记录仍属于第二/三阶段。当前没有宣称完成这些能力。
