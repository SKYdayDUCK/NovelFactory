# 数据库、事务、并发与恢复审核

审核对象为 NovelFactory 及随仓的 Agent Workshop。基线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`，交付开发提交 `602ba7fe8ece18503ac3b994adee4f00634cd761`，实际合并提交 `1de471076b8fb4240f95ff8033c60db34c187404`。以下行号均指实际合并提交。独立复读实际源码、测试及 GitHub Actions 的作业日志。当前审核环境没有可用的本地 PostgreSQL 执行器。文中的运行结果明确归于其实际检出版本。

## 数据所有权与约束

| 受保护的数据 | 写入及校验 | 仍需依赖的边界 |
|---|---|---|
| WorldRef、分支与历史 | `infrastructure/database.py:46–66` 通过复合外键保证作品／分支／版本一致。分支序号唯一，延迟校验的头部外键在事务提交时生效。 | Harness 的候选不直接改变世界头。S2 确认通过原生选稿接口更新计划选择。 |
| 文稿、上下文及创作工件 | `database.py:88–97,147–150` 的工件修订主键、内容对象外键、任务代际唯一上下文；`kernel.py:121–143` 写对象、引用和不可变 JSON。S1 快照用 context_bundle_id 派生固定修订。 | JSON 内嵌的 ArtifactRef 没有逐字段数据库外键；生产服务核对 work、kind、修订和摘要。有权限的恢复运维仍须一起验证对象存储。 |
| 请求重放和模型费用 | `database.py:117–121` 的 `(work, principal, command_type, idempotency_key)` 主键配合 `kernel.py:520–528` 的请求摘要；`reliability_tables.py:95–114` 的 `(task_id,generation,call_key)` 唯一约束与预算外键。 | 重放按旧内容核验，换键不自动解除 UNKNOWN；长时间历史由数据库保存。 |
| 跨服务委托与收件 | `delegation_tables.py:51–71` 使用 installation/operation 复合键、唯一 task、receipt、slot 与 reservation；Workshop 保存本地组织回执。 | 两个服务各自提交，无跨库事务。恢复时查双方现有回执及账事实质；只有 UUID 相同仍不足以放行。 |

[表与引用](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/infrastructure/database.py#L39) · [NF 委托](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/infrastructure/delegation_tables.py#L51) · [实际结算表](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/infrastructure/reliability_tables.py#L84)

## Migrations 与回退

- NF 0019 对 NARRATIVE_DRAFT 创建按 work 与父稿修订的部分表达式索引。在 `creative_context_store._check_sources:80–88` 和 `creative_review._current_manuscript:584–591` 用同一表达式筛出已被后继稿取代的输入。留存的 0018→0019→0018→0019 夹具中，两份草稿摘要逐步相同，计划显示索引条件匹配 work/parent，branch 为后续过滤。证据是小夹具的计划，不是大库延迟测量。[迁移](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/migrations/versions/0019_p57_context_sources.py#L18) · [实际索引计划](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/evidence/s1/migration.json#L1)
- NF 0020 新增可空 `nf_audit_events.harness_details` 和 `nf_artifacts(work_id,created_at,artifact_revision_id)`。历史行保持 NULL。降级先取 ACCESS EXCLUSIVE，再检查已有非空观察并在有数据时抛错，避免检查后写入和删列。索引为常规事务式建索引，构建期间会阻塞对 artifacts 的写入；新增列也需要 audit_events 的 DDL 锁。历史升级测试先记录旧行，升级后比较各字段，新增真实编译观察后请求降级，验证失败且记录逐字段相等。上线维护窗需按实际表尺寸安排。[迁移](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/migrations/versions/0020_p57_harness_observations.py#L14) · [历史升级反例](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_harness_log_upgrade.py#L17)
- W011 替换远端回执投影函数，并在插入 Skill 快照时经 org/task 与 operation/delegation 关联追加历史。旧 cut 不回填；已有 READY 回执与第一次认领可有一代差，`receipt_generation` 记录 AW 已知水位。w011→w010 明确要求恢复成对备份。已填充 w010 的演练逐行比较变更日志及 source cut，再检查直接降级拒绝。[迁移](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/migrations/versions/w011_harness_observations.py#L13) · [水位断言](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/integration/test_workshop_harness_log_upgrade.py#L36) · [实时限定](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/src/agent_workshop/dynamic_skills.py#L98)

## 锁、幂等、并发反例

| 路径 | 已核验的实际机制 | 正例或反例 |
|---|---|---|
| S1 固定包 | `AgentGateway._messages:353–364` 重新读取当前绑定；`freeze_snapshot:180–199` 取 task UPDATE，检查已选材料、已有快照及固定修订，再同事务登记。 | 同时提交相同内容复用旧记录；并发计算结果不同会拒绝。负控的过期草稿检查在 PG 执行，恢复后的陈旧代际仍要重新验证。 |
| S2 计划和推演 | `CreativePlanningService.propose:220–258` 在分支锁、task guard 下连同原生候选和回执提交；`decide:271–316` 取 work UPDATE，查已有终局，采用原生 CAS。模拟根据候选使用事务级 advisory lock 及确定的 run ID，再取分支/规划任务并按 task_id 顺序重验参与人。 | 两个同时提案得到同一结果，两种采纳并发只成功一次；竞争同一候选的不同演算参数，1 成功、1 IDEMPOTENCY_MISMATCH，库内只 1 run/1 report。撤权发生在本地演算后时，无 report/run 写入。 |
| S3 编辑与提交 | `creative_review.py:60–65,424–450,477–490` 先取 work UPDATE，检查稿件、审阅绑定、上一决策，再同步更新 action 指针、原生编辑记录及回执。 | 同一审查的 DEFER 可继续决策，RETURN 后可撤回，其他终局禁止二次覆盖。独立再审后才计入原生 ACCEPT_REVISION。 |
| S4 人员和 Skill | 指针更改在 work UPDATE 下检查 expected_epoch；`dynamic_agents.py:707–752` 在远端核验外重新获取写锁并对比原修订。`load:754–836` 固定 task/gen、原始输入、W4 安装和 grant。 | 有界并发测试验证竞争指针单一获胜；活体 W4 在送出前撤权，断言请求 0、当前 PREPARED 结算 0；在模型结果之后撤权，断言 WAITING_REVIEW、正式接受 outbox 和候选均无新增。 |
| S5 优化和恢复 | `adaptive_evaluation.py:489–517,534–553,619–675` 以候选/测量确定 ID 和事务锁分步记录，已有样本核对 start_ref，重复不再运行该测量。正式发布的控制写操作 `adaptive_promotion.py:79–89,459–495` 以作品行锁、epoch 和 selection 管理版本。 | 恢复可以复用已经完成的测量；不完整、超时的结果可保留，发布仍须另行核查。旧任务使用原冻结。 |
| S6 观察/outbox | `audit_log.record:54–77` 对 event_id 做 ON CONFLICT，只有插入时才追加 audit.recorded，二者同一连接和事务。消费者忽略 audit.recorded 自身（:88–92）。Outbox claim 使用 SKIP LOCKED，递增 generation，inbox 复合键幂等、ack 校验租约（`tasks.py:614–665`）。 | 可以重复运输；本地数据库 handler 与 inbox 同事务。模型或跨服务 HTTP 不享有数据库内部的 exactly-once 语义。PITR 后来自未来时间线的 Redis 条目进入隔离。 |

[规划竞争实测](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_harness_planning_simulation.py#L147) · [推演竞争及撤权实测](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_harness_planning_simulation.py#L335) · [真实 W4 迟到投递](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/integration/test_dynamic_late_delivery.py#L73) · [共享约束和重试](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/orchestration/tasks.py#L568)

同一候选得出终局后，再次调用原提案/演算接口有可能在读取旧回执之前触发 STALE_PLAN 或 PLAN_SELECTION_CONFLICT。源码区分“资格仍有效时重试”和“历史产物查阅”，例如 `creative_planning.py:67–80,220–231`、`creative_simulation.py:297–303`。测试已经覆盖撤权后拒绝旧回执和决定自身精确重试。尚无在纯终局变化后调用同键并要求返回旧响应的明确契约。未把该观察列为丢失回执。

## 费用、代际与故障恢复

1. `TaskService.reserve_call:500–541` 在当前 task、budget 行保护下检查 work/currency、任务总额和 Token，数据库唯一键约束 Attempt。发送前后使用不同状态。取消只有在与 mark_call_sent 相同的任务行锁下才能释放**同代 PREPARED**，按 budget_id、attempt_id 的固定顺序结算；SENT 和 UNKNOWN 预留保留。重复取消可以清理旧软件遗留的同代未发送预留。[取消及锁顺序](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/orchestration/tasks.py#L325)
2. `_recover_task:403–436` 将当前代 SENT 转为 UNKNOWN，存在未知/准备中调用便暂停并推进代际；`resume:447–461` 再查所有代际。真实测试把 40/32/28 Token 分别放入 PREPARED/SENT/UNKNOWN；取消后只有 40 释放，余 60。重复提交 .20 / 18 Token 发票后仅结算一次，余 28 保留。另一个恢复案例断言旧代 PREPARED 与 UNKNOWN 合计的 100 仍被占用。[测试](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_shared_token_budget.py#L220)
3. 选定恢复点后，服务启动前隔离 active 委托；保留等待屏障、推进未终结任务代际、标记 SENT 为 UNKNOWN、使 DELIVERING outbox 重新可交付。旧代 PREPARED 在外部实际发送情况不明时仍被保留。原生结算禁止 PREPARED 直接记为非零终值（`tasks.py:587–600`）；需要另行使用正确时间线/外部账单开展核对。此次演练没有实测从该精确夹点导入外部正额发票。该限制在合并前代码中同样存在。[恢复隔离](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/infrastructure/recovery.py#L59) · [同代及旧代测试](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_shared_token_budget.py#L266)
4. 实际 main 的 `P5.7 pitr` 日志给出 LSN `0/312CA60`，pg_verifybackup 成功，WAL 归档 4、失败 0，隔离前 71 张表与 39 个对象核对。业务目标点保留 1 个 UNKNOWN 和 0.5 预留，4 个任务被隔离，故意排除 1 个后继世界提交。Redis 7.0.15 回放保留 1 个订阅回执、1 个审计行，隔离 1 条晚于目标的消息。指定恢复点年龄 0.57975 s、恢复至经核验服务 1.41071637 s。S1 快照重算相等、旧角色原始读取拒绝，但该用例 `preserved_source_count=0`，不证明含多份正文来源的物理恢复。[main 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764/job/131113456588) · [恢复后验证函数](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/scripts/p57_pitr.py#L78)

## DB-01 · P2 · 两个独立 WAL 源的恢复尚未取得实测回执

**证实的差异**　`reports/harness/S6_ACCEPTANCE.md:12` 称 paired-restore 恢复两个系统的独立备份。实际 `workshop/integration/test_paired_restore.py:95–139,250–285,389–399,476–519` 仅初始化一个物理 PostgreSQL 源，用 `database_pair(admin_dsn)` 在源内创建业务/组织两库，从同一 basebackup/WAL 取多个恢复点。它确实把恢复的其中一侧与另一侧较新的服务进行双向核验，测到 WAITING_OPERATOR、UNKNOWN 保留、发票幂等和禁止重复提交。另一个 dual suite 在 `workshop/integration/conftest.py:18–28,45–79` 比较两个独立集群的 `system_identifier`，测试在该拓扑下正常委托、撤销及权限操作。两项证据覆盖不同故障。

**复核条件与结果**　对照两次进入 `database_pair` 的实参和 `pg_basebackup` 的次数；物理测试读取的两个逻辑库有共同的源实例。留存 paired-restore 回执两个命名目标的目标/回放 LSN 分别相同（`0/4029B00`、`0/4087058`），仅 1 个环回模拟请求，真实供应商调用 0。该回执属于开发资格 SHA `a7f41ae7d32ad322fe48895950b0245b3096fee6`。main 的双服务作业报告 11 passed；main 的 P5.7 pitr 是上段独立的 NF 恢复用例。

**影响与状态**　部署到两个独立 PG 集群后，各自归档延迟、独立基备缺失或恢复目标偏移仍未获这组物理演练覆盖。未观测到数据损坏。建议先收窄验收说明，再从两个不同 system_identifier 各自做 basebackup，分别指定两个本地恢复点，覆盖操作水位领先及落后、重复查询、真实费用未知侧继续持有预留。当前为测试/文档 P2，未修改生产事务或恢复语义。[实际测试拓扑](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/integration/test_paired_restore.py#L95) · [成对回放回执](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/evidence/s6/paired-restore.json#L1) · [资格作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43195702902/job/131105980975) · [main dual](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764/job/131113456930)

**保留与规模限制**　`ObjectStore.collect:167–197` 仅清理已过宽限期的无引用对象；业务工件、含稿件的快照、账单及审查历史均继续受引用保护。大型历史库的 DDL 阻塞、读隔离持锁时长、生产 PG/Redis 的积压分布和并发工况下的死锁频率没有独立实测；未观察到足以指认本增量造成死锁或重复付费的证据。关联性能结果见 [PERFORMANCE_COST_REVIEW.md](PERFORMANCE_COST_REVIEW.md)。
