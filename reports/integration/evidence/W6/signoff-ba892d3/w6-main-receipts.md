# W6 合并提交的原始工程回执索引

验收对象为 PR #15 的合并结果 `98a8bbed289a29d0326454c86dd3db54ac85ae5c`。冻结候选 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771` 与合并提交的 Git tree 均为 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。以下数字均取自该主线 SHA 的 push 工作流。PR #25 是另一次文档签收，需在其合并提交上另记复验。[W6 合并](https://github.com/SKYdayDUCK/NovelFactory/pull/15) · [签收 PR](https://github.com/SKYdayDUCK/NovelFactory/pull/25)

## 主线六个工作流

| GitHub run | 结果 | 实测范围 |
|---|---|---|
| [Workshop 43122517485](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517485) | 成功，3 jobs | 业务、迁移、双服务和双账恢复，消息与原始场景，匹配准入策略与扩展场景 |
| [NF 43122517275](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517275) | 成功，2 jobs | schema/unit 398 通过，PG17 完整套件 817 通过、17 跳过、1 warning；另有实际 HTTP、Redis、冻结六场景工件 |
| [物理恢复 43122517281](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517281) | 成功，1 job | PG17 物理备份、强杀、WAL 显式目标与应用核对 |
| [P2 43122517430](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517430) | AT-30 NOT_MET | 50/200 mixed 成功；50/200 overload 失败 |
| [P3/P4 43122517311](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517311) | AT-30 NOT_MET | 固定世界 CPU/SQL 对照及 200 mixed 成功，独立 200 overload 失败 |
| [P3 43122517551](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517551) | AT-30 NOT_MET | 500 mixed / 500 read-soak 成功，1000 开环读的 capacity 与 wire-rate 均 NOT_MET；7 个 transport errors |

[Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) 跟踪原始容量门槛。员工委托完成数和 NF 高压吞吐门槛分属不同测试。

## 已执行的测试和跳过项

- Workshop 原始 JUnit 分组为 84 项一般业务、27 项 W4 审核/技能/升级、3 项后续 W4 升级、4 项 W5 升级、8 项 W6 历史升级、W1/W2/W3 历史组分别 1/3/2 项、NF 工具组 5 项、服务与数据库隔离 2 项、双服务恢复综合 4 项，各组零失败、零错误、零跳过。受限 NF 委托组 18 项、双服务桥接组 1 项，均通过。证据分别为 `workshop-results.zip/*.xml` 和 `nf-bridge-results.zip/{nf-delegation,dual-service}.xml`。CI 的一般组忽略的审核套件在单独命令执行。参见 [一般及专项测试](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L156)。
- 原始场景 job 的目录隔离与 Workshop 中断为 2 项、Minimal/Standard 为 2 项、fixed-1 为 3 项。对照 job 的 Long-form/Research-Audit 为 2 项、fixed-2/adaptive-2 为 6 项。四个场景与九个委托组合皆有 JUnit 测试记录。两个 job 使用互补的 pytest `-k`；对照 job 的 wheel 与 Redis 消息步骤有意跳过，原始 job 独立执行成功。见 [矩阵入口](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L387) 和 [各模式选择器](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L442)。
- NF 的 `postgres-all.xml` 实际为 834 项，其中 17 项在该 runner 跳过，原文均为 `Linux container setup denied by this runner`。其中 13 项属于 `test_p5_actor_tool_choice`，2 项属于 `test_p5_role_container`，2 项属于 `test_p5_supervised_role_read`。逐个 case 名可查原始 XML。该组角色工具选择、namespace 资源隔离与 supervisor 受限读取不能据本次 JUnit 记为通过。[环境检查](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/tests/integration/test_p5_role_container.py#L23)

## 双 PostgreSQL、MCP 与四个场景

- 两个 PG17.11 服务容器、各自服务账户和独立数据库、Redis 8.0.2；分别运行两个 Alembic 的 upgrade head。八个 W6 来源用各自发布代码建库并填充，再迁移到当前版本；来源为 w5c、w006、w007、w008、w008a、rehearsal_early、rehearsal_late、w009。见 [数据库拓扑](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L46)、[迁移](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L152)、[八个历史来源](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/acceptance/test_w6_historical_upgrades.py#L33)。
- `workshop-results.zip/codex-dual/receipt.json` 记录同一 Codex 原生角色线程使用两个 OpenSSH forced commands 连接服务。NF/Workshop SHA 都为本页主线 SHA，PG system identifier 分别为 7707064918721019943、7707064914277609510，服务 UID 分离，2 次跨服务 binding 读取被拒，0 次模型轮次。另有独立角色和组织的权限、9 项 NF 工具、真实双 stdio 的提交与丢回执恢复。见 [原生角色检查](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/agent-workshop.yml#L273)、[丢回执反例](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/integration/test_dual_stdio.py#L40)。
- 四套预设的场景 JSON 存在 `workshop-scene-recovery-results.zip/ci-evidence/scene-{minimal,standard}.json` 与 `workshop-comparisons.zip/ci-evidence/scene-{long-form,research-audit}.json`。目录与 BELIEF 隔离的正反例在 `at43.json`，Workshop 进程被杀且 Workshop 库拒绝连接、其间 NF 完成工作、恢复后只结算一次的回执在 `at49.json`。原文与模型输出均由工程 fixture 提供。[场景执行](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/cross_service_scene.py#L169) · [权限反例](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/actor_directory_isolation.py#L19) · [中断场景](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/running_workshop_outage.py#L65)

## 10/50/200 员工

| 员工 | 发送且确认的唯一消息 | 发送 P99，ms | 确认 P99，ms | Redis 流重建，s |
|---:|---:|---:|---:|---:|
| 10 | 30 | 210.75 | 183.90 | 0.71 |
| 50 | 150 | 340.84 | 231.34 | 2.70 |
| 200 | 600 | 174.47 | 141.95 | 12.25 |

三档各有一个 JSON 输入/结果集，均使用 SQL-backed authenticated MCP dispatcher、8 workers、3 rounds，每五条复投。消息时延排除 stdio 传输；各档移除 Redis 流后从 SQL outbox 恢复全部 hint。见 `workshop-scene-recovery-results.zip/ci-evidence/messages-{10,50,200}.json`、[测量实现](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/message_capacity.py#L123)。

| 员工及任务数 | fixed-1 耗时，s | fixed-2 耗时，s | adaptive-2 耗时，s |
|---:|---:|---:|---:|
| 10 | 2.82 | 3.04 | 3.93 |
| 50 | 13.72 | 13.98 | 22.07 |
| 200 | 105.69 | 120.45 | 202.53 |

九档均为 N 个唯一任务、N 次接受、N 个结算、N 次本地合成 HTTP 请求、0 个未结预留、0 个额外预算授权，各档实际模型调用并发峰值均为 1。fixed-2 与 adaptive-2 的配置上限为 2；测试只证明闭环积压在规定的 600 秒内完成。排队时间计入组织请求到回执结算的时延。三个 fixed-1 JSON 在 `workshop-scene-recovery-results.zip/ci-evidence/delegations-N.json`，其余六个在 `workshop-comparisons.zip/ci-evidence/delegations-N-POLICY.json`。源码与门槛见 [委托矩阵](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/delegation_capacity.py#L91)、[固定单并发契约](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/delegation-contract.json)、[对照契约](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/benchmarks/admission-comparison.json)。

## 物理备份、两库错位和 UNKNOWN 预算

- NF 独立 P4 演练使用 pg_basebackup、pg_verifybackup、SIGKILL、WAL replay。原始 `p4-real-pitr-1.zip/pitr/result.json` 为 PASS，目标 LSN 为 `0/310B750`，到目标丢失 0 次提交，特意排除之后 1 次提交，保留 1 次 UNKNOWN 和金额 0.5，隔离 3 项 task，拒绝过期 worker。校验覆盖 70 张表、31 个对象，恢复至应用验证用时 0.872 秒。此为本机小样本的回放时间。[P4 工具](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/scripts/pitr_drill.py#L189) · [工件完整性检查](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/.github/workflows/p4-recovery-drill.yml#L58)
- 配对演练使用独立临时 PG17 实例，在该实例中创建 NF 与 Workshop 两个数据库，由同一物理备份在两个时间点恢复，再同 live 实例的对应数据库配对。代码环境与前述独立生产形态双服务容器有区别。原始 `workshop-results.zip/paired-restore.json` 记录备份成功，位点 `0/402B9A8` 与 `0/407C500` 均实际回放到相等 LSN。三个错位情形包括① NF 落后，在线 Workshop 保存原占用与回执，进入 WAITING_OPERATOR；② Workshop 落后，读取在线 NF 的原委托后恢复 BOUND，UNKNOWN 继续 HELD，明确合成账单后结算 0.020000；③ 结算回执号相同但 NF 缺失 attempt，双方冻结，已结算金额保留。6 次恢复调用皆为原 operation 的 nf_delegation_get，1 个最终任务，1 次本地合成 HTTP、0 次供应商调用。[物理演练入口](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/integration/test_paired_restore.py#L70) · [备份](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/integration/test_paired_restore.py#L237) · [三种错位](https://github.com/SKYdayDUCK/NovelFactory/blob/98a8bbed289a29d0326454c86dd3db54ac85ae5c/workshop/integration/test_paired_restore.py#L391)

## 下载的原始工件

以下七份 GitHub ZIP 经完整下载并逐份校验 SHA-256。原始下载可从 GitHub 工件链接取得。已选取的原始回执、来源与成员哈希见 [持久化回执包](w6-selected-receipts.zip)。

| 工件 | GitHub artifact ID | SHA-256 |
|---|---:|---|
| [workshop-comparisons](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517485/artifacts/13901499191) | 13901499191 | `c589f5a92d8d9444bac3d8c08afe0997900333cee2277172728f80aa8385da2e` |
| [workshop-scene-recovery-results](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517485/artifacts/13900938944) | 13900938944 | `89ef88281583c69b3d493f576e8803123621a33fa9719c04387ad194784ce77f` |
| [nf-bridge-results](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517485/artifacts/13900494389) | 13900494389 | `1483658b0f0ffa9980ccfd9e780332e4fa3f8c94b89cdcdcecdfe8672fed6161` |
| [workshop-results](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517485/artifacts/13900334763) | 13900334763 | `c1e27ffc186ac3ecec8d036487dcc72cffa8c33eaa9beeb46458b09d93c0b9f3` |
| [p1-postgres-concurrency-1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517275/artifacts/13902131066) | 13902131066 | `a8b3cb64c840d8040e3b2737f0fe68ab7dffd9d4aa9ac17d25515947e50cf670` |
| [p1-schema-unit-1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517275/artifacts/13901527343) | 13901527343 | `4a069d5ae3fd84be6124f576da41baf7a9111fa02823ba25b15410af098eb023` |
| [p4-real-pitr-1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43122517281/artifacts/13901397117) | 13901397117 | `e55654888cebb4ad685757d179d1ea19471209b15e89ceaec8f20c642afc2238` |
