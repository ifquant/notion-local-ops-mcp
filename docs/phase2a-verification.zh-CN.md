# 第二阶段 2A 实现与验收

日期：2026-10-15

结论：**2A 本地验收及本次约定的真实 Notion MCP 功能验收通过**。已验证稳定工作身份、创建请求重试、双项目读写隔离、精确 LF 写入、跨对话找回运行中及已完成任务、五项越界/错误归属拒绝和同一任务的增量终态读取。基线为 `441728e`，实现位于当前工作区，并已滚动部署至测试服务；测试连接获授权按原配置重建，没有提交/push。按用户确定的边界，Notion 消息发送失败仅作外部现象记录，不纳入 MCP 缺陷判定。本结论不代表完整第二阶段或长期无人值守运行已验收。

## 已交付接口与存储

- `open_workspace` 登记已有项目并返回稳定 workspace_id；`start_run` 保存目标、验收条件和创建键；`get_run` 读取身份与项目可用性。
- 18 个项目/任务工具接受可选 run_id。文件、搜索、补丁、技能、Shell 和 Git 使用工作根目录解析路径；任务查询、等待和取消从持久化关联确定归属。
- SQLite schema 1 只包含 workspaces、runs、task_runs。现有任务 JSON 和日志不迁移，数据库不保存第二份执行终态。checkpoint、列表和任务提交去重由后续小阶段扩展。
- 存储目录/数据库权限为 0700/0600；输入上限 64 KiB、创建键上限 128 UTF-8 字节、数据库上限 64 MiB、单次锁争用等待上限 2 秒。异常返回存储错误，不删除原记录重建。
- 创建 run 的响应丢失可用原 creation_key 重试；这不代表后台命令提交已经幂等。提交命令的响应丢失仍须核实，不能盲目重跑。

使用约定与参数示例见 [中文 README](../README.zh-CN.md#第二阶段-2a稳定的工作身份)。

## 要求与验证对应

| 2A 要求 | 验收证据 |
| --- | --- |
| 项目身份稳定，路径别名归一，不合并不同 worktree | 两个独立 OS 进程各重复登记/创建 20 次，最终只有一个 workspace 和一个 run；真实 Git linked worktree 得到不同 ID |
| 一个项目可有多个 run，创建请求可重试 | 相同创建键返回原 run，不同参数冲突，不同创建键创建独立工作；重开存储仍可读 |
| 显式 run 不受全局 cwd 干扰 | 两线程交错读取、两 HTTP 客户端交错写入不同项目；相对 cwd 从各自根目录解析；旧默认行为仍通过回归 |
| 越界目标被拒绝，错误不会导致部分应用 | 批量读取、写入、补丁移动、多文件新增、Shell cwd、委派 context_files、Git 路径和项目技能检查；多文件补丁后项越界时首项也未写入 |
| 任务先持久化归属，再执行 | 两种后台命令入口及委派入口均验证；归属保存失败时无进程启动；get/wait/cancel 拒绝不匹配的 run |
| 状态真相与身份记录分离 | 新 registry 可读旧结果但 control_available 为 false；旧任务标为未关联；任务 JSON 缺失时报读取失败，不从 SQLite 伪造终态 |
| 项目移动、替换或不可访问不自动重绑 | 项目可用性变为 false，项目操作拒绝；已关联任务仍可查询；项目消失不阻止读取终态 |
| 跨进程并发、版本和失败保护 | 双进程唯一约束、锁等待超时、未知 schema、损坏数据库/记录、建表中途失败回滚、容量耗尽、权限错误及危险链接拒绝 |
| 旧调用兼容，阶段能力不夸大 | 第一阶段全量回归、MCP schema 和能力标记测试；带 run 的调用不发送到旧全局 relay；中英文 README 与 Agent 指令已更新 |

主要实现：[workspaces.py](../src/notion_local_ops_mcp/workspaces.py)、[server.py](../src/notion_local_ops_mcp/server.py)、[pathing.py](../src/notion_local_ops_mcp/pathing.py)、[tasks.py](../src/notion_local_ops_mcp/tasks.py)。

专项测试：[test_workspaces.py](../tests/test_workspaces.py) 与 [test_work_scope.py](../tests/test_work_scope.py)，共 42 项参数展开后的测试。

## 测试结果

最终全量命令：

```bash
env -u ALL_PROXY -u all_proxy -u HTTP_PROXY -u http_proxy \
  -u HTTPS_PROXY -u https_proxy .venv/bin/python -m pytest -q
```

结果：**263 passed，1 warning，23.72 秒**。其中包含 42 项新增专项测试及原有 221 项回归测试。warning 为既有 Starlette TestClient 的 httpx 弃用提示。

HTTP 验收使用临时目录、临时 localhost 端口及测试 Bearer token，覆盖两个独立 MCP 客户端和重新连接后的身份/任务读取。去除代理变量只作用于测试命令，不改变系统代理或服务配置。

验收过程还修正了旧技能测试未恢复全局替换函数的问题，避免测试顺序影响结果。SQLite 临时 journal 的检查使用单次 lstat，避免其他进程提交时删除 journal 导致虚假存储故障。

## 真实 Notion 验收

2026-10-15，在 `if quant’s Space` 的“Phase 2A MCP验收-A对话A”与独立的既有“Phase 1 本地验收 MCP测试”对话中，使用 `Local Code Agent — Phase 1 Test` 连接测试。鉴权保持不变，验收端没有降低审批设置。用户重试期间，任务启动所在对话曾显示 DeepSeek V4.1 Flash / Skip all approvals；边界检查对话保持 Review for me，取消探测经单次确认后执行。因此不将所有步骤描述为同一模型或同一审批配置。

- 测试服务已滚动重载，隧道保持不变。本地与公网端点的缺失/错误 Bearer 请求均返回 401；有效凭证可读取 27 个工具。此项为独立客户端连通性检查，不替代 Notion 操作证据。
- Notion 实际调用 `server_info` 成功，返回 27 个工具，`run_scoped_paths` 与 `task_associations` 为 true。服务日志请求 `f931cc793f` 与会话反馈相符。
- 首次 Notion 报告无法调用 `open_workspace`、`start_run`、`get_run`，管理页只有旧版 13 个读工具和 11 个写工具。未找到原地刷新入口；经用户确认断开，用户重新填写原令牌并连接后，管理页变为 14 个读工具和 13 个写工具，写操作保持 Always ask。原会话随后实际调用三个新接口，工具目录阻塞已解除。
- 对话 A 两次调用相同 `start_run`，返回同一 run；SQLite 只存在一个工作记录，归属项目 A。创建请求对应日志 `d3a44d5448`、`d64349287f`。
- Notion 使用 run_id、不传 cwd 读取 A 的 identity.txt、写入并回读 result.txt，后台命令也在项目 A 目录启动；后续双项目检查见下文。
- 首次文本保真出现输入偏差：要求 `A_SCOPED_WRITE_OK` 后接换行，Notion 实际发送 content 为 `A_SCOPED_WRITE_OK<br />`。服务请求 `dd49ebbe45` 与本地字节一致，HTML 尾缀在进入 MCP 时已存在。后续已由 Notion 修正，并完成独立的精确 LF 测试；不归为 MCP 的内容转换缺陷，不通过服务端替换合法 HTML 掩盖输入偏差。
- Notion 仅启动一次 `run_command_stream`，随后 `wait_task` 读到 `PHASE2A_STARTED pid=46750`，在任务仍运行时回复 task_id 和 next_cursor。对应请求为 `e0edd43450`、`e3ccdb0276`。没有使用 delegate_task 或内置云端终端。

### 跨对话与双项目验收

12:39 至 12:44（Asia/Shanghai），两个新会话、既有独立测试会话和 Chrome 网页端均出现 `Error sending message`。已尝试界面 Retry、刷新以及拆短为只读查询；这些后续查询没有到达 MCP 服务。网页端打开新会话时还显示 No messages or tool calls yet。消息发送故障不能仅归因于桌面客户端，具体网络、账号或 Notion 服务原因未确定。

12:50 至 12:54，用户确认消息发送恢复后，独立的既有测试对话实际完成 get_run/get_task，读出 A 的目标、项目目录、归属及 succeeded/exit_code=0 终态；get_task 只传 task_id 和 cursor=start，服务自动附带原工作归属。请求 `8a0a783ff6`、`8b9360cf42` 与 Notion 回复一致，日志 has_more=false、output_truncated=false。

同一独立对话随后登记项目 B，临时把全局 default_cwd 设为 B，在不传 cwd 的情况下分别用 B/A run 读取各自 identity.txt，写入并回读各自 result.txt。两文件均为精确 17 字节、无换行无 HTML；A 原有 HTML 尾缀已通过 Notion 修正。最后恢复 session_cwd=null 并 get_default_cwd 确认 source=workspace_root。相关请求 `abebcda7ec`、`b07524e045`、`b584f31520`、`b937f4e78e`、`baaf353796`。这确认了默认目录干扰下的双项目读写隔离。

12:59，Notion 使用 A run 写入 newline-probe.txt，content 为 `alpha\nbeta\n`，请求 `fff34b6dd5`。回读显示两个 LF；本地十六进制核对为 `61 6c 70 68 61 0a 62 65 74 61 0a`，共 11 字节。独立测试脚本在后台启动时也通过了精确字节断言。

随后 Notion 仅启动一次新的 verify_resume.py，任务 `410f3da1c228`，PID `64273`；wait_task 读到 STARTED 后停止。切回另一个对话后，不传 run_id 的 get_task 自动返回 A 的归属和 running 状态，实际请求为 `4196e5c280`，证明跨对话找回的是同一个运行中任务。

五项真实调用均返回 `success=false / scope_conflict`：

| 操作 | 服务请求 |
| --- | --- |
| A run 读取 ../project-b/identity.txt | `43aa21e5e5` |
| A run 写入 ../project-b/should-not-exist.txt | `47083a3738` |
| B run 查询 A 的运行中任务 | `48a4ce33b0` |
| B run 等待 A 的运行中任务 | `4ae006d1a8` |
| B run 取消 A 的运行中任务 | `83fe234f87` |

取消探测首次被 Notion 自动审查拦截，没有到达服务；明确单次可丢弃测试任务的副作用范围后重试，MCP 实际拒绝。随后 A run 查询 `860290e7b1` 与本地 ps 均确认原进程仍运行；越界目标文件未创建。未关闭该对话的审批保护，也未改用 A run 取消任务。

13:09:16，本地验收控制端释放第二个测试门闩。Notion 用 STARTED 后的原 cursor 调用 wait_task（请求 `9311d1107a`），增量 stdout 仅有 `PHASE2A_RESUME_FINISHED original_task=1`，stderr 为空，返回 succeeded、completed=true、exit_code=0、has_more=false、execution_timed_out=false、cleanup_failed=false。本地任务记录和日志一致，resume-launches.txt 只有一行，PID 已退出。跨对话查询没有重新提交命令。

首个任务 `dd46c41b2aaa` 也只启动一次，于 12:44:50 释放门闩后成功结束，随后已由独立 Notion 对话从头读出终态。两个后台测试均已正常收口，无遗留测试进程。

### 接入现象与结论边界

重连解决了旧工具目录缓存问题，后续新增接口需要检查 Notion 的工具目录是否同步。消息发送的间歇失败通过重试后继续，不以未到达 MCP 的请求判定服务失败；本次不分析或修复 Notion 消息服务。成功的 MCP 操作仍须结合任务记录核实，尤其后台提交在响应不明时不能盲目重跑；2A 尚不提供任务提交去重。

### 实测身份

- workspace_id：`12f4a242521c4dc6b11e26337a087909`
- run_id：`9afcae39a4a24d49a6e300c0583091a7`
- task_id：`dd46c41b2aaa`
- STARTED 后 next_cursor：`eyJ0YXNrX2lkIjogImRkNDZjNDFiMmFhYSIsICJvZmZzZXRzIjogWzI2LCAwXX0=`
- B workspace_id：`dcfe2b1464ee46c7a46c7b305b13a3a0`
- B run_id：`67f65e6d11eb44a9ad046389a0156ec1`
- 第二个独立测试 task_id：`410f3da1c228`
- 第二个任务 STARTED 后 next_cursor：`eyJ0YXNrX2lkIjogIjQxMGYzZGExYzIyOCIsICJvZmZzZXRzIjogWzMzLCAwXX0=`

测试夹具与未提交源码快照保存在本机 `notion-mcp-phase2a-test.0GLVoo` 临时验收目录；源码归档 SHA-256 为 `43b1666c9da1523815a2a209843e871cb2c4efecfe0e1b921785e3c25eb4007c`。凭证与公网入口不纳入本报告。

## 尚未包含的保障

本次不是完整第二阶段或真实 Notion B13 验收。checkpoint 恢复、工作/任务列表、任务提交去重、编辑版本保护和按工作路由 relay 尚未实现；后台进程跨重启接管仍属于第三阶段。

工作范围检查防止误用项目，但不是安全沙箱，也不提供与外部文件改动之间的原子性。Shell、委派程序和 Git hook/config 仍有服务账号权限。带 run 的 Git 工具要求登记范围包含仓库根目录；全局技能摘要仍由 include_global 控制。

首次 schema 初始化有事务回滚与版本检查；尚无需要执行的数据版本升级。数据库容量耗尽时保留记录并拒绝新增，工作记录完整删除/归档流程不在 2A 内。
