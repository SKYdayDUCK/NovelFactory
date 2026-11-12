# P5.6 双服务综合验收

本轮从 W6 签收后的精确主线 `ba892d3a424be39efccb697232a5614b26609cc4` 启动。NF 与 Workshop 分别使用 PostgreSQL 17.11、受限服务身份和 stdio MCP。角色场景使用本地确定性 HTTP 响应，外部供应商调用数为零。[基线提交](https://github.com/SKYdayDUCK/NovelFactory/commit/ba892d3a424be39efccb697232a5614b26609cc4) · [签收原件](https://github.com/SKYdayDUCK/NovelFactory/tree/9eb7bb52e422f1a8e33a91fe01a1e67fef6375d3/reports/integration/evidence/W6/signoff-ba892d3)

## 工程基线实测

| 维度 | 本轮基线结果 | 证据范围 |
|---|---|---|
| 正确性 | PASS | 世界、并发、规划、正文及局部修订 10 个模块共 89 项通过，其中含本阶段增加的场景修订回归。 |
| 权限与安全 | PASS | 实际服务身份、路由、角色知识、Skills 冻结/撤回及独立审核。托管宿主的 17 项环境跳过保留在 NF 作业的原始结果。 |
| 故障恢复 | PASS | 原委托恢复、重复消息、UNKNOWN、Redis 重建、两向账本错位和独立物理 WAL 回放。 |
| 工程集成 | PASS（下文所列基线场景） | 四种组织预设、两项隔离/中断、双服务和已部署数据库迁移。 |
| 历史容量 | NOT_MET | 原有 AT-30 高压力档位保留其阈值、原始测量和 Issue #13。 |
| 文学质量 | NOT_EVALUATED | 工程夹具未接入作者授权的真实创作模型与独立审读。 |

[本轮基线及新增用例的原始 JUnit、覆盖矩阵与恢复回执](https://github.com/SKYdayDUCK/NovelFactory/tree/7f129c59fac03850d8269549cdc2ea21e1fdbbcd/reports/integration/evidence/P56/baseline-ba892d3)。本地委托的完整九档、GitHub 候选和精确合并主线各自有独立的运行身份。[阶段 B 回执及正式签收入口](https://github.com/SKYdayDUCK/NovelFactory/tree/evidence/serial-delivery/reports/integration/evidence/P56)。

## 组织、世界与审核

| 验收范围 | 独立实测与判据 |
|---|---|
| 组织与通信 | 对组织岗位、员工雇佣、调任、离职与复职、授权版本、目录可见性及实际路由执行 Workshop 全套业务测试。路由回执含 28 条有向路由、154 个无边组合，79 次允许、439 次拒绝。 |
| 跨服务创作 | 在干净的基线工作树重新执行 Minimal、Standard、Long-form、Research-Audit 四种预设，以及角色私有认知隔离和 Workshop 运行中断两项。6 项全部通过，角色场景工件记录两端源码和内容哈希。Standard 路径从作者、管理者、编剧、总导演、卷纲导演到演员，随后进入独立审核，并将相同正文版本的结果回给作者。 |
| 世界与局部修订 | 冻结 WorldRef 后按各角色认知读取、提交行动。不同计划产生可复查的分支，涉及过期前提的任务通过 NF 冲突处理；未受影响场景继续推进。新增数据库反例在两份正文各自对账成功之后变更一个规划节点，验证旧审核无法放行受影响的正文，独立场景仍能幂等接受，世界版本和事件数保持一致。 |
| Skills 与审核 | 核对五种分享策略、任务冻结哈希、历史版本与实时撤回、跨作用域读取拒绝、新任职身份。审核绑定原文版本及内容哈希，独立于产出者。验证自审拒绝、有限返工、独立决策、审核费用预留和 NF 正式接受入口。 |
| 服务隔离 | 两个实际 PostgreSQL 实例使用不同集群 ID。测试在有效 stdio 会话上撤权，验证交叉绑定和跨库登录均被拒绝，以及接收端丢回执后客户端沿用原 operation ID。 |

## 故障恢复与开销

基线本地 Workshop 业务测试 109 项通过、1 项跳过。该项依赖 Docker stop/start。跨服务集成另外 4 项通过。Redis 8.0.2 的原生进程丢失与重建经过两次独立 run ID 核实，两次均从零键启动，恢复后同一 AW 事件和三条 NF 任务事件全部可读，原预算预留继续持有。Docker 路径由 Workshop 托管作业执行。

双账本恢复演练以一个物理备份为源，在包含两个独立数据库的当前测试集群上恢复至两个命名位置，再分别与在线服务配对。两个目标与实际回放的 LSN 对应为 `0/402B9A8` 和 `0/407C500`。当 NF 恢复得较早，Workshop 保留原回执及占用，等待核对。当 Workshop 恢复得较早，系统找回原任务，合成账单将状态从 UNKNOWN/HELD 推进到 SETTLED，金额为 0.020000。相同回执号下若缺少 attempt，原结算金额保留，恢复栅栏继续生效。

数据库迁移另用干净基线工作树、PG17 受限账号及 8 个空目标库执行，涵盖 W5c、W006、W007、W008、W008a、两套同名 rehearsal 与 W009，共 8 项通过。各用例先以实际历史提交写入数据，再用当前代码连续执行两次升级，校验原回执、权限、预留以及重启后的消费行为。

NF 独立演练另行执行真实 pg_basebackup、pg_verifybackup、进程强制终止及 WAL 回放，结合 Redis 与数据库恢复检查。目标 `0/310B670` 前丢失提交数为零，此后的一次提交按设计排除。核对 70 张表、31 个对象，保留一笔 UNKNOWN 和 0.5 单位的预留。该次本地夹具从恢复启动到应用核验耗时 1.77 秒。

10、50、200 名员工按原消息合同各完成 3 轮，对应 30、150、600 条唯一消息全部确认。200 人档发送 P99 272.30 ms，确认 P99 154.23 ms，Redis 消息重建 29.85 s。该合同测量 SQL 支撑的认证消息分发，未计入 stdio 传输。

NF 高压容量按原 AT-30 条件保留每一档结果。W6 的 50/200 overload 和 1000 read-open 等历史红项继续由 [Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)跟踪。授权额度、回执和 Token 计量是工程验证对象。合成样本的文学质量记录为 **NOT_EVALUATED**。P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION。

## 复核入口

- 组织与岗位使用 `workshop/tests`，四组跨服务恢复与预算使用 `workshop/integration`。双库创建、受限身份、迁移、Redis 和原始测试命令见 [Workshop 工作流](../../.github/workflows/agent-workshop.yml)。
- 四个预设运行 `workshop/benchmarks/cross_service_scene.py`。角色目录与停机行为分别见 `actor_directory_isolation.py` 和 `running_workshop_outage.py`。消息与委托按已有容量合同的参数执行。[原合同和基准代码](../../workshop/benchmarks)
- NF 独立恢复执行 `python scripts/pitr_drill.py --with-redis --output <新的临时目录>`。权限、世界、规划及正文测试由真实 PG17 完整作业执行。[物理恢复流程](../../.github/workflows/p4-recovery-drill.yml) · [NF 完整性工作流](../../.github/workflows/p1-integrity.yml)
- 将同次 GitHub Workshop 工作流的 run、jobs 和 artifacts 原始 REST 响应命名为 `run.json`、`jobs.json`、`artifacts.json`。作业查询使用 `filter=all`，收齐 jobs 和 artifacts 的所有分页并保留总数。同一运行部分重跑时按每个作业的最新实际执行归属。下载原始 `workshop-results.zip`、`workshop-scene-recovery-results.zip`、`workshop-comparisons.zip`。执行 `python workshop/qualification/verify_run.py --metadata-dir <API目录> --archives <工件目录> --checkout-sha <实际检出的40位SHA>`。PR 使用合成合并提交，主线使用真实 push SHA。退出状态和 JSON 保存到本阶段证据目录。
