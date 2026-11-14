# Dry Run 与程序生命周期

轻量管理器是普通程序。它监听事件、查询任务资格、按需分配 Worker 并回收空闲资源，不用大模型轮询或判断是否启动进程。大模型承担授权角色的规划、写作和审核任务。

## 离线流程测试

在仓库根目录运行：

```powershell
python scripts/nf.py dry-run --seed 42
```

此入口默认 `novel` 场景，成功目标是整书最终 RELEASE 冻结。使用内存任务状态、虚拟时钟和固定种子的随机审核，虚拟等待不消耗真实等待时间。完整阶段为世界与卷/场景规划 → 角色行动 → 裁决 → 场景写作 → 认知/事实核查 → 对账 → 章节审读与主编决定 → 章节冻结及连续性摘要 → 整书组装 → 结构/接缝/跨卷编辑 → 双路独立全文审核 → 终稿主编决定 → 整书最终冻结。

章节退回后生成新候选并重新做事实核查；连续退回触发 Codex 策略任务，每个章节最多八轮策略批次，超过边界仍返回失败。整书退回后生成新编辑版、重新核对事实、重新编辑及双路审核。两位读者必须针对同一版终稿，最终冻结清单绑定所用章节、读者与主编回执。程序不会为了完成测试强制把随机退回改为通过。

调用经过真实的 `RuntimeModelAdapter.generate` 和 `agent_runtime.providers.invoke`，`ModelRequest.dry_run=True` 传到 Harness 执行边界后进入模拟后端。`ModelResponse.dry_run=True` 和 `TEST_FIXTURE` 标记随结果返回。复用生产的审核门禁及 Worker 生命周期组件；数据库调度状态使用内存实现。本模式不执行生产 SQL、真实模型推理或文学验收。

原生 Codex/DeepSeek 路由在模拟记录中仍可见：规划和审核为 Codex，写作为 Flash。dry run 不读取 API Key、不检查真实 CLI 登录、不启动外部模型进程、不连接 PostgreSQL/Redis。进程审计钩子阻止 socket 与子进程启动，报告检查数据库及凭据模块未导入。即使环境中存在真实数据库地址和生产配置，也不会采用它们。

### 常用命令

随机审核，模拟三书各五章：

```powershell
python scripts/nf.py dry-run --scenario random --books 3 --chapters 5 --review-pass-rate 0.7 --seed 42
```

验证完整成功通路：

```powershell
python scripts/nf.py dry-run --scenario normal --review-pass-rate 1 --require-complete
```

连续种子与异常矩阵：

```powershell
python scripts/nf.py dry-run --scenario all --runs 50 --seed 42 --books 3 --chapters 5 --agents 5000 --workers 4 --max-reworks 2 --output dry-run-report.json
```

`--agents` 是模拟逻辑身份数量，不创建同数量的进程，不构成真实 5000 Agent 容量证据。`--workers` 是模拟并发上限。`--output` 创建新的 JSON 文件，拒绝覆盖已有文件；`--trace` 加入每次任务状态、Harness 请求与响应的关联记录。`--format json` 将完整 JSON 输出到标准输出。相同参数和种子可重放；改变种子可探索不同审核序列。

### 判定与退出码

| 字段或退出码 | 含义 |
| --- | --- |
| `status=PASS` | 调度不变量检查通过，包括异常被正确拦截 |
| `workflow_status=COMPLETED` | 配置的模拟流程已完成；小说场景要求所有书最终冻结 |
| `frozen_books` / `requested_books` | 最终冻结的整书数 / 配置书数 |
| `releases` | 终稿版次、章节清单哈希、两位读者与主编身份及所审版本 |
| `workflow_status=STOPPED_WITH_BLOCKERS` | 正确停在预算、授权、对账或返工上限，未虚报完成 |
| 退出码 0 | 流程检查通过；默认 novel/random 还必须达到整书完本冻结 |
| 退出码 1 | 不变量失败、达到步数上限，或 `--require-complete` 下未完成 |
| 退出码 2 | 命令参数或报告输出路径不合法 |

例如通过率为 0 时，正确行为是达到返工上限后停止；异常场景的不变量检查可 PASS，但工作流不是 COMPLETED。默认 novel/random 及带 `--require-complete` 的命令会使这种情况返回非零退出码。模拟预留额度以 `SIMULATED_CALL_RESERVATION` 为单位，不是供应商账单。

### 异常场景

`--scenario all` 覆盖 38 类场景：整书自动推进、空闲、正常推进、首次章节审核退回、持续退回、限流、超时、显式对账后恢复、发送前退出、发送后退出、无效 JSON、输出截断、通知丢失、重复通知、重复结果、迟到结果、租约失效、重启、撤权、预算耗尽、AND/OR 依赖、存储断连、Worker 池损坏、上下文不匹配、整书审核退回、合法 NEEDS_INPUT、成功响应携带外层未决问题、内部问题核查后同稿复读或改变策略、审核缺 MCP 证据的有限补读或耗尽、未知核查调用、旧报告来源核查、世界覆盖恢复、过程阻塞后的唤醒、多章覆盖切面交接真实世界语义变化的拒绝，以及依据未完成剧情续写、重审和完本。NEEDS_INPUT、未决问题及 UNKNOWN 保留原调用，不靠重发制造进展。故障矩阵允许正确停在人工权限或未知对账边界；不能把矩阵 PASS 解读为其中每个故障流程都已完本。

`stale_finding_provenance` 模拟读取旧报告的确切版本来源后，对同稿重做独立事实审读，不直接令作者重写。`world_coverage_recovery` 区分未求值与未登记，模拟程序闭包后用新切面完成同稿核查与对账。`world_coverage_blocked` 加入已登记过程阻塞：等待期间不派发新的模型请求，不机械推进，依赖完成后自动唤醒。三类场景的工具回执均明确标为 `DRY_RUN_MEMORY`，正式 MCP、PostgreSQL 闭包和版本续接由独立集成测试证明。

`multi_chapter_coverage` 在每章形成独立覆盖版本，第二个 Writer 继续保留原输入切面，经限定的前驱入口验证同批次、同场景、同计划、同稿接受回执及纯覆盖祖先链，再消费前章的新切面接受。多章冻结和全文编辑分别复核所有章节原切面到目标切面的兼容证明，保留原 source/read WorldRef，不把它们重写为一个版本。纯覆盖证明逐步核验模拟的正式接受、`SCHEDULE_ADVANCE` 闭包、空事件/记录/触发操作，以及 canon、registry、解释与分支身份；重新计算 hash 不能绕过这些条件。事实仍为 PENDING 或 UNKNOWN 时不能变成 CLEAR。`coverage_semantic_changed` 模拟真正的世界内容变化，正确结果是等待正式重验证，不能继续当作纯覆盖。

下面是可复现的多章压力配置：50 个连续种子、每轮两本各八章、随机审核通过率 0.7、有限返工上限 12，并要求全部最终冻结。返工上限是本次模拟参数，不是对真实生产额度的更改。对应验证另包含前驱血缘、混合切面、语义/登记/解释变化及非空记录操作的拒绝反例。

```powershell
python scripts/nf.py dry-run --scenario multi_chapter_coverage --books 2 --chapters 8 --runs 50 --review-pass-rate 0.7 --max-reworks 12 --require-complete
```

检查包括并发上限、接受幂等、依赖门禁、身份/上下文绑定、未知调用预留保留及禁止重发、无孤儿 RUNNING 任务和无未解释的死锁。模拟器不会自动批准随机失败以获得成功报告。模拟对账证据显式标为模拟，不能用于真实账本结算。

## Harness 的模式隔离

`ModelRequest` 新增默认关闭的 `dry_run` 和可选 `DryRunSpec`。旧调用保持真实执行语义。模拟运行环境的 journal 标记 `dry_run=True`，请求和环境模式必须一致；模拟模式禁止指定真实 wire receipt 文件。旧的 HTTP/原生宿主适配器对不支持的 dry-run 请求在发送前拒绝，不能回退为付费调用。

真实 AgentGateway 与 actor lookup 拒绝模拟响应；模拟请求不通过生产 HTTP/MCP 参数获得绕过计费或授权的能力。真实模型允许列表 `NF_RUNTIME_ALLOWED_MODELS` 在凭据读取与上游调用前检查。当前本机允许 DeepSeek Flash 和已配置的 Codex；旧冻结 P6 启动器也先检查当前宿主允许列表。

## 生产 Worker 生命周期

`scripts/run_supervisor.py` 默认用纯程序 `AgentLifecycleManager` 按需创建执行池。未派发任务时不创建池，最后一个在途任务结束后按 `--idle-seconds`（默认 45 秒）回收，固定并发和自适应模式均适用。监听器持续存活；通知批量合并，断线重连后补扫。阻塞结果进入等待，不紧循环调用模型。异常 Future 留诊断并由原任务/Attempt 恢复规则处理，管理器不重复执行它。

```powershell
python scripts/nf.py supervisor --access-file PRIVATE_ACCESS_JSON --idle-seconds 45
```

P6 原实验入口增加 `worker --watch`：预期暂停后保留轻量监听，仅在权威调度输入变化时重入 Worker；观察日志及同内容产物的新 UUID 不触发付费重跑。截止时间、租约、权限、候选、预算和对账变化仍被跟踪。源码、模型与对象目录继续按冻结清单校验。此模式不跳过业务门禁，也不自动解锁 UNKNOWN 或超预算任务。

运维启动器支持 `--stop-file PRIVATE_PATH`。在该私有文件中写入 `{"action":"STOP_AFTER_CURRENT_CALL","book":"A","source_sha":"实际冻结提交"}` 可请求在当前付费调用完成后退出；不能用强杀进程来更新运行源码。配置文件的 `active_models` 按作品绑定新阶段配置，旧 `models` 清单保留历史兼容，两个层次均校验冻结配置和宿主模型允许列表。

生产服务的数据库恢复、操作系统托管、真实模型回执和正文质量需要各自的集成验收。离线 PASS 不代表生产小说已完成，也不代表实际吞吐或供应商恰好一次执行已得到证明。

## Windows 持久宿主

`scripts/p6_host.py --config PRIVATE_HOST_JSON plan|install|start|status|stop` 将已冻结的本地 P6 Worker 交给当前用户的 Windows 计划任务。私有配置只包含 `version`、源码 worktree 的绝对路径 `source`、恢复目录 `state`、本地部署配置 `deployment` 和 `books`（本次为 `["A","B"]`）。不登记 API Key；模型和数据库继续从当前用户凭据库读取。

先 `plan` 核对版本，再 `install` 注册并 `start` 启动。宿主串行启动本地服务，确认原 P6 数据库和 Redis 健康后派发；状态以实际文件锁和数据库健康为准。它保持系统唤醒，支持登录后恢复和至多三次进程失败重启；调用重试仍由业务层判断，UNKNOWN 不会被宿主重发。`stop` 写入绑定源码和作品的优雅停止请求，等待当前调用结束。

`local_runtime.py` 的私有部署配置可选 `postgres_clusters`，每项指定 `owner`、`data_directory` 和 `port`，用于启动恢复出的独立 PostgreSQL 17 集群。原有普通 PostgreSQL/Redis 配置保持兼容。当前计划任务使用登录用户的凭据和 Codex 登录状态，需要该用户保持登录；断电或注销期间不能生成。

2026-11-14 较早一轮双书离线验证：连续 50 组两本各五章全部最终冻结，另有 27 类共 1,350 项故障检查通过。[历史证据](../../reports/dry_run/20261114-ab.json)。后续多章版本验证使用两本各八章、50 个连续种子及 37 类共 740 项异常检查，数据库和上游模型调用仍均为零。[多章证据摘要](../../reports/dry_run/20261114-multicut.json)。生产返工诊断、独立复审和有限额度使用单独的 PostgreSQL 集成测试核验。
