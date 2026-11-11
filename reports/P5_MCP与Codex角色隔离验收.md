# P5 R3｜角色 MCP、Codex Host 和执行隔离

接管基线为 `73cdc997759c39e3e5dffe0ef0c4e0a42970f93f`。检查 `McpRuntime`、`CodexAppServerAdapter`、`AgentGateway`、正式 MCP 服务、既有 Host/IDE 回执后，保留已有工具与权限逻辑。角色生产路由对一个预留调用只转发一次文本；原来的 Host 明确拒绝工具请求，开发机上直接启动绑定服务器也无法隔离相同账户下的文件。

| 项目 | 结论 | 证据与适用范围 |
|---|---|---|
| 独立授权、角色 BELIEF 与故事时间 | DONE（本轮 PG/MCP） | 冻结的岗位/技能快照、任务 grant、当前租约、generation、WorldRef、actor、permission epoch 和 policy version 共同约束工具。标准 MCP 客户端覆盖绑定互换、过期、取消、知识平面切换和查询清单边界。 |
| 角色工具宿主 | DONE（单个角色控制通道） | 实际 Codex CLI 0.167.0-alpha.4 在独立容器中创建临时 Thread。真实服务器只列出四个工具，能读本角色上下文、提交候选，并在撤权后拒绝访问。 |
| H 认知隔离 | PARTIAL | 已测跨角色/超范围查找与不存在对象返回同一错误、禁止切到 PLAN/WORLD/READER、BELIEF 的未知值无数据、跨任务绑定拒绝、候选重放。Host 工具列表无任意材料、图邻居、缓存查询或计划入口。未对全部标题、关联元数据、缓存状态与时间侧信道做成对统计检验。 |
| J 标准 MCP 与真实 Codex | DONE（协议、工具） | Python SDK 1.29.0／协议 2025-11-25，当前分支复测。实际 Host 的工具调用由受信测试控制器驱动，全程零模型 Turn。 |
| J 官方 IDE | DONE（合成模型的官方 Web 与原生 Linux 桌面） | 普通用户 UID 1001 在官方 VS Code 1.145.3、官方 Codex 26.51106.51428 侧栏完成两次中文恢复；可查真实 MCP 工具清单、两次服务器回执及数据库确认。商业模型与生产 SSH 账户另需对应部署记录。[桌面原始记录](../evidence/p5/final/native-desktop.json) · [Web 工作台历史回执](../evidence/p5/mcp_vscode_extension.json) |
| M OS 执行隔离 | DONE（等效 Linux 容器，非双账户 SSH） | user、mount、PID、IPC、UTS、network 命名空间，清空环境、删除 capabilities、禁用嵌套 user namespace。静态探针在相同边界内无法看到服务文件、服务环境或 shell，无法连接本轮真实测试 PostgreSQL。 |
| 按需模型自主选择工具 | DONE（有界合成供应商）；真实商业运行 BLOCKED_ENVIRONMENT | R3c 的显式新路由允许模型选择一次角色查询或跳过，真实 MCP 工具与两次独立模型账本连接到普通 Supervisor。详见本报告 R3c 与运行记录。 |
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

## R3c 模型选择工具并读取后继续

显式配置 `codex-role-tools` 的角色现可在自己的第一个模型轮次决定是否读取一条状态。模型输出一个严格的工具选择对象，控制器通过真实的隔离 Codex Thread 将其转换为 `mcpServer/tool/call`。本次服务器仅开放 `nf_query_batch`，与该角色被冻结的运行时工具、清单、grant、租约、代际及 BELIEF 相交。第二个独立模型轮次从已获准的观察生成最终行动。选择不查询时省去 MCP 查询和观察工件。

| 验收 | 实测 |
|---|---|
| 正常自主查询 | 两次模型发送各有不同持久 call key 和 Codex Thread/Turn。模型选中的查询返回角色未知状态，后一次请求准确携带该观察且行动候选通过校验。 |
| 跳过、超范围与错误工具 | 选择跳过时没有查询产物。选择真实但未授权对象时停止在读取，选择非白名单工具时确定性拒绝。两者都不发送后续模型请求。 |
| 收费与恢复 | 对任一步的未知费用或结果保留 UNKNOWN，不自动重新发送。已知成本的无效选择记为 FAILED 并按其实测回执结算。服务端在原任务额度内分别预留两笔，并复核每次发送权限。第一步成功、读取前中断时，在仍有效的同一 worker 租约重入可复用已持久选择；已失效 generation 按原恢复围栏暂停对账。 |
| 成本证据范围 | 反例与端到端使用确定性供应商夹具、真实 Codex 进程和 PG17。合成计费仅验证账本。OpenRouter 可从官方响应中精确读取单请求美元扣费，支持范围及精度见接入说明。外部商业模型和发票核对仍为 BLOCKED_ENVIRONMENT。 |

[逐步 Host、MCP 和账本回执](../evidence/p5/verification/actor_tool_choice.json)与[正反例](../tests/integration/test_p5_actor_tool_choice.py)。本路由将模型可选择的动作限定为零次或一次只读查询，后续模型提交领域候选。官方工作台、普通桌面、角色间任意工具对话及开放式多轮循环各有独立的验证范围。

本轮 R3c 联合回归在有真实 Codex、PG 17.11 和可用命名空间的主机运行，43 项通过，33.83 秒，零跳过。第一次把临时数据库放在共享内存文件系统，WAL 写入因磁盘已满失败；改用独立工作区的临时目录后完成上述检查。

## 独立反例复核

同一代和同一 worker 实际并发重入时，第一步发送权仅领取一次；在查询观察已保存后撤销 grant，最终请求被拒绝；作品余额只能负担第一步时，第二次预留被拒绝。非法 UUID、超长谓词和错误工具名均保留已知消费并停止该任务。共享物品只在管理员明确授权的本场进入裁决工具清单；接受同时校验实际持有者和所有者。双赢家、未列明接收人和已授权配置中的错误持有者都未改变正式历史。角色自身没有继承裁决者的物品授权，后续场景也没有继承这笔赠予授权。

复核发现第一步模型声明输出 257 Token、超过该步 256 上限时，即使输入输出总和仍在预留下，旧路径会发送第二个请求。修复后按已知费用结算失败的选择，保留账本并阻断后续发送。真实 PG 和 Codex 的角色选择与联合裁决复核 26 项通过，42.59 秒，零跳过。其中包含先前已执行的用例，计数独立。[逐用例清单](../evidence/p5/verification/actor_tool_choice.json)

## 原生 Linux 桌面验收

2026-11-11，提交 `1823668b7ab2180c21bfc8a71a84e8191735dd05` 在 Ubuntu 24.04 通过。扩展自带 Codex CLI 0.167.0-alpha.4.1，PG 17.11，回环合成 Responses 服务。第一次唤醒一项，第二次重放唤醒零项。下载的 artifact `13838650376` 的 ZIP SHA-256 经本地重新计算为 `63eee32b824e8b9b3b9445985dfd3c60050a86ba8405e0a6b2e21186479dbeee`，与 GitHub 回传值一致。[Actions 原始运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42982135338) · [过滤后的原始回执](../evidence/p5/final/native-desktop.json)

PR 中改动 [原生桌面脚本](../scripts/probe_vscode_desktop.py)、[MCP 观测夹具](../scripts/probe_vscode_offline_transport.py) 或 [对应工作流](../.github/workflows/p5-native-desktop.yml) 时，GitHub Actions 的 “P5 native desktop resume” 会运行。需要在另一台 Linux x64 主机复验时，按工作流的三个步骤安装依赖、下载并校验桌面和 VSIX、在独立回环网络中运行 Xvfb 和临时 PG17。桌面及 PostgreSQL 均以普通用户运行。只有建立测试网络、安装系统依赖和 Electron 沙箱助手需要管理员权限。

从该次运行的 Artifacts 下载 `p5-native-codex-desktop-<run_id>-<attempt>`，打开 `native.json`。成功条件包含官方桌面和已安装扩展的版本、正常侧栏中两次输入“继续未完成的任务”、调用连接实际发现两个限定工具，两次 `nf_resume_project({})` 的唤醒数依次为 1 和 0。数据库须只有一个事件确认、一个就绪后继，作品侧供应商调用数为零。桌面用独立临时 HOME 和 CODEX_HOME，模型只连接测试命名空间中的合成 Responses 服务。该同账户夹具测量原生桌面到本机 MCP 的路径。作者使用独立 SSH 服务账户时仍需按[首次接入说明](../adapters/codex/P5-stdio.md)配置强制命令与项目绑定。
