# S6 生产日志与证据关联

NF 继续在 `nf_audit_events` 记录任务、场景、工具及模型证据。默认的静态路径与启用 Harness 的路径都记录原生编译和实际模型调用。S1 至 S5 在创建对应业务工件的同一事务中追加事件。回滚会撤销同一事务的日志写入。已有 `audit.recorded` outbox 只传范围与标识，重放使用同一事件 ID。

| 事件 | 观察位置与字段 | 解释 |
|---|---|---|
| `context.compiled` | 固定任务代际的原生编译成功后，记录包字节数、许可证据数量、章节数量、省略范围数和单调时钟耗时 | 字节数是整个原生 CompiledContext 的规范 UTF-8 JSON 大小。复用原始 outbox ID，异步消费不会再写一行。 |
| `harness.context.packet` | 权限验证后的材料读取、整理、角色边界复查和快照创建，记录 `duration_ms`、`packet_bytes`、`source_count`、`included_count`、`omitted_count` | 字节数是实际角色 user 消息的规范 JSON，包含 assignment。计数分别是被选证据源、材料分类、省略记录。没有附带省略的内容。缓存命中复用原始快照。 |
| `harness.plan.candidate/decision`、`harness.simulation.completed` | 候选数、选中项或整组拒绝、停止原因分布、步骤、参与者数、真实局部工作量与模型用量 | `branch_count` 包含产生停止状态的选项。真正开始的分支数由 `rollout_attempts` 给出。只记录汇总数量，角色各自的立场留在受权业务证据中。 |
| `harness.review.completed/decision/revised` | 审核层、轮次、问题总数、固定问题类型计数、决定与报告引用 | 具体评语、引文和修订文本由各自受权原件保存。 |
| `harness.agent.revision/selection/frozen/recovered` | 动态实例/版本/历史引用、选择代际、员工绑定、雇佣版本、配置摘要和 Skill 版本 | 建任务前的冻结行以 `provisioning_task_id` 和 `provisioning_generation` 表示预分配身份。真实任务行存在后，审计外键指向该行。 |
| `harness.instruction.candidate/evaluated/reviewed/decision/selection/monitored` | 变更类别、评估结论、版本/候选/证据引用、独立意见、批准/拒绝/回滚、批准时的功能开关 | 输出来自已经提交的服务工件。既有任务引用已冻结版本。发布和回滚不会写世界事件。 |
| `model.completed`、`mcp.started/finished`、`harness.task.recovered` | 模型/尝试 ID、冻结 Prompt 引用和摘要、用量/金额/币种/核算状态、耗时、安全错误分类。超期/恢复时记录前后代际、状态和恢复方式 | 模型证据及调用账本保留 UNKNOWN 与未知金额。MCP 按请求首尾测量，包含阶段内的外部等待。配置文件、调用参数、原始模型返回和提供商异常字符串均不写入新增日志。 |
| `harness.delegation.bound`、`harness.skill.verified/denied` 与 Workshop `aw_remote_receipts`、`harness.skill.frozen/withdrawn` | 对账用 `installation_id`、`operation_id`、`receipt_ref`、`binding_id` 和可用的 WorldRef/任务代际 | Workshop 的 `receipt_generation` 是该库已经收到的 NF 水位。一次 READY 到首次 claim 之间可能相差一代，NF 审计保留实际验证的任务代际。撤回事件与之后 NF 的拒绝通过同一个 Skill 绑定关联。 |

NF 的现有列提供 work、branch、world revision、task、generation、attempt、role、event time、trace 及证据引用。在具有 AgentInput 的事件上，附加字段从该代际读取原先冻结的四个开关、运行时摘要、角色和 Skill 版本，能识别静态、固定 Harness、动态员工与自适应选择。语义版本可读，操作人提供的自由文本版本及非 UUID 员工标识使用稳定 SHA-256。模型事件的 `prompt_sha256` 指向实际送入本次请求的受保护记录。精确费用仍按同一 NF Attempt 对账。历史行缺少测量的字段保留 NULL。

## 查询与权限

NF 管理员通过 `LogService.trace(kernel, principal, work_id, branch_id, trace_id)` 或既有授权 trace 入口查询。人员和角色端不获得额外读取能力。查询再次核验目标分支 ADMIN，并在 SQL 中同时约束作品、分支和 trace。场景内任务沿用现有场景 trace。独立任务用 task ID 作 trace。

Workshop 的 `aw_history_get` 使用现有认证员工会话和签名游标。经理只能在当前组织和安装范围内读取新冻结/撤回及委派回执；普通员工还须匹配其雇佣代际。归档、恢复后，已存在的事件仍携带最初证据引用。外部 Workshop 验证期间，NF 不持有写事务。

## 历史库

NF 迁移 `0020_p57_harness_observations` 增加可空 JSONB 元数据与作品时间索引。索引构建会等待写锁，大库应选用写维护窗口。旧数据不回填时间、费用或材料数。已有新增观察后退回 0019 会拒绝丢失数据。在原版结构上运行的升级演练会按当前数据库目录判断观察列是否已经存在。

Workshop `w011` 更新已有远程回执投影，首次任务 Skill 冻结时才写入相应安装的事件。已签名的旧水位、行和游标不重写。卸载采用成对备份恢复。

[生产读取与费用日志](../../src/novel_factory/application/audit_log.py) · [分阶段字段投影](../../src/novel_factory/application/harness_observations.py) · [NF 真实库测试](../../tests/integration/test_harness_observations.py) · [两端委派与撤权](../../workshop/integration/test_workshop_harness_observations.py) · [NF 历史升级](../../tests/integration/test_harness_log_upgrade.py) · [Workshop 历史升级](../../workshop/integration/test_workshop_harness_log_upgrade.py)
