# P5 R3｜角色 MCP、Codex Host 和执行隔离

接管基线为 `73cdc997759c39e3e5dffe0ef0c4e0a42970f93f`。检查 `McpRuntime`、`CodexAppServerAdapter`、`AgentGateway`、正式 MCP 服务、既有 Host/IDE 回执后，保留已有工具与权限逻辑。角色生产路由对一个预留调用只转发一次文本；原来的 Host 明确拒绝工具请求，开发机上直接启动绑定服务器也无法隔离相同账户下的文件。

| 项目 | 结论 | 证据与适用范围 |
|---|---|---|
| 独立授权、角色 BELIEF 与故事时间 | DONE（本轮 PG/MCP） | 冻结的岗位/技能快照、任务 grant、当前租约、generation、WorldRef、actor、permission epoch 和 policy version 共同约束工具。标准 MCP 客户端覆盖绑定互换、过期、取消、知识平面切换和查询清单边界。 |
| 角色工具宿主 | DONE（单个角色控制通道） | 实际 Codex CLI 0.167.0-alpha.4 在独立容器中创建临时 Thread。真实服务器只列出四个工具，能读本角色上下文、提交候选，并在撤权后拒绝访问。 |
| H 认知隔离 | PARTIAL | 已测跨角色/超范围查找与不存在对象返回同一错误、禁止切到 PLAN/WORLD/READER、BELIEF 的未知值无数据、跨任务绑定拒绝、候选重放。Host 工具列表无任意材料、图邻居、缓存查询或计划入口。未对全部标题、关联元数据、缓存状态与时间侧信道做成对统计检验。 |
| J 标准 MCP 与真实 Codex | DONE（协议、工具） | Python SDK 1.29.0／协议 2025-11-25，当前分支复测。实际 Host 的工具调用由受信测试控制器驱动，全程零模型 Turn。 |
| J 官方 IDE | PARTIAL | 基线的官方 VS Code Web 1.145.3、扩展 26.51106.51428 录到了工具发现及两次恢复，唤醒数 1、0。该记录使用回环合成模型。普通用户桌面安装、受限 SSH 实例和商业模型本轮为 BLOCKED_ENVIRONMENT。参见[历史 IDE 回执](../evidence/p5/mcp_vscode_extension.json)。 |
| M OS 执行隔离 | DONE（等效 Linux 容器，非双账户 SSH） | user、mount、PID、IPC、UTS、network 命名空间，清空环境、删除 capabilities、禁用嵌套 user namespace。静态探针在相同边界内无法看到服务文件、服务环境或 shell，无法连接本轮真实测试 PostgreSQL。 |
| 按需模型自主选择工具 | PARTIAL | 原生产 Supervisor 继续使用 `codex-text`。新 `run_role_host.py` 提供角色专属真实工具通道；生产模型的每个工具后续推理尚无独立的供应商预算/回执与恢复接线。保留现有单请求消费约束。 |
| 真实付费模型及费用 | BLOCKED_ENVIRONMENT | 本轮没有调用商业供应商。模型质量、Token 及账单不能由本次 Host 工具调用推导。 |

## 实现与负向检查

[角色 Host 入口](../scripts/run_role_host.py) 创建一对私有 FIFO；在容器外以单角色模式运行原有 `McpRuntime` 服务。角色授权、绑定文件和 DB 对象均留在外部。容器中仅挂载管理员指定的两个静态二进制文件及本次 FIFO，工作区和临时目录按大小限额建立。用户空间保持相同数字 UID，边界由 Linux 内核命名空间与挂载集合实现。环境不满足时入口失败，不会自动切回共享开发进程。

[真实容器测试](../tests/integration/test_p5_role_container.py) 先在实际 app-server 发现 `nf_task_get`、`nf_context_get`、`nf_query_batch`、`nf_artifact_put`，然后读取自己的 actor_id；分别查询未授权的真实与虚构 subject，检查两次均仅返回 VIEW_DENIED。相同意图提交两次产生一份候选，Canonical HEAD 保持原版本。撤权后同一线程再次读取即失败。系统探针只使用生成的测试文件、环境值和隔离库端口；不会读取真实管理员资料。服务侧的权限判定沿用现有数据库事务。

## 复验

```bash
uv run --no-sync python scripts/with_postgres.py --migrate -- \
  .venv/bin/python -m pytest tests/integration/test_p5_mcp.py \
  tests/integration/test_p5_role_container.py tests/integration/test_agent_gateway.py -q
```

使用 Python 3.12、PG 17.11、mcp 1.29.0、实际 Codex 0.167.0-alpha.4、静态 C 工具链、util-linux 及 bubblewrap。2026-11-11 本轮专项 20 项通过，耗时 14.19 秒，零失败、零跳过；两项新增容器测试均实际执行。[逐用例记录与源码指纹](../evidence/p5/verification/role_container.json)没有命名空间或缺少可执行文件的环境会明确跳过对应容器实验，此类 CI 不能代替 M 的验收。部署步骤和单会话传输限额见[接入说明](../adapters/codex/P5-stdio.md)。

## R3b 普通派发入口的角色读取

新增显式路由 `codex-role-mcp`。登记时选用此路由的角色任务经 `RuntimeSupervisor.run_once` 正常领取，冻结输入后，隔离 Codex Thread 使用仅开放 `nf_context_get` 的服务读取角色上下文。返回值经逐字段比对构成实际供应商请求，仍在发送事务中复核授权。场景中的其他岗位使用相同路由时沿用原单次文本派发。旧登记保持旧路由。

正例使用真实 Codex 工具 Thread、独立的模型 Thread/Turn、PostgreSQL 和定序 HTTP 模型。一个模型账本记录对应一个 HTTP 请求，产物通过 ActionProposal 校验；空闲再恢复时没有新增请求。反例移除角色转发器，任务在模型预留和发送前停止，账本与 HTTP 调用计数均为零。该工具由服务选择；多轮模型根据工具结果继续推理需要额外、独立的预算与发送回执。[派发实测](../evidence/p5/verification/supervised_role_read.json)
