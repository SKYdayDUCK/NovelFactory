# 问题台账

基线为 `1de471076b8fb4240f95ff8033c60db34c187404`。P0 为严重安全或数据完整性问题，P1 为重要正确性、权限、恢复或关键验收回归，P2 为可界定的性能、设计、观测或资格缺口，P3 为次要维护问题。此次共 9 项：5 项 P1 已修复，4 项 P2 延期，未确认 P0 或 P3。以下每项的代码 SHA 代表发现时的不可变代码。最终主线提交与专项原始回执另见 [合并回执](MAIN_MERGE_RECEIPT.md)。

## P57-SEC-01 · P1 · S1 / Gateway 制品访问

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/kernel.py`，273–307，349–360，`Kernel._check_artifact_read / Kernel.get_artifact` |
| 相关来源 | `src/novel_factory/application/agent_gateway.py:374–393` · `src/novel_factory/application/dynamic_runtime.py:112–130` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/kernel.py#L273](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/kernel.py#L273) |
| 描述 | 单独启用 S1 的模型提示、响应及候选等网关材料缺少可供通用读入口复核的创作分支归属。 |
| 触发条件 | 同作品的管理员限于另一个分支，测试持有对应合成制品的已知引用。静态对照和动态快照分别检查。 |
| 复现及观测 | 对合成稿件通过普通 Gateway 准备及完成一次工作，再核对授权策略返回值，旧格式回执使用原生审计关联。比对 ALLOW/DENY，不向报告输出稿件。 |
| 实际影响 | 受影响的是已知引用且有同作品管理员授权的原始创作数据。未观察到匿名读取、跨作品或角色模型 MCP 的裸读。 |
| 修复建议 | 在新网关阶段保留位置和代际，旧制品从受信任的冻结任务及审计回执恢复来源，读前、读后都核验该分支授权，保留静态兼容性。 |
| 状态 | 已修复，定向原树/修树及最终主线记录可交叉核对。 |
| 复核 | [原树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202876684/job/131130521377) · [修复树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202876684/job/131130521753) · [开发与主线补丁映射](REMEDIATION.md) |

## P57-SEC-02 · P1 · S2 / S3 / S4 来源授权

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/creative_planning.py`，17–48，275–311，`CreativePlanningService.decide / CreativeReviewService._current_review` |
| 相关来源 | `src/novel_factory/application/creative_review.py:383–448` · `src/novel_factory/application/mcp_runtime.py:158–197，262–278` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/creative_planning.py#L17](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_planning.py#L17) |
| 描述 | 候选与审稿完成后，最终控制器路径复核 NF grant、代际或审稿独立性，却漏查产生结果的动态员工当前是否仍获 Workshop 的对应 Skill。 |
| 触发条件 | 正常授权产生候选或签名审核后，仅撤销源 Skill，保留 NF grant、在职关系和控制器授权。 |
| 复现及观测 | 在两个独立 PostgreSQL 集群上经 W4 正常授权、委托和撤权，再调用 NF 正规计划与编辑决定端口。核对原生计划指针及编辑记录。 |
| 实际影响 | 原实现可以采信没有当前来源权限的工作结果，改变计划指针或登记编辑决定。此两次复现都没有正式世界事件或商业调用。 |
| 修复建议 | 在 NF 写事务外取当前 W4 见证，在最终业务事务中同时验证任务代际、冻结绑定、工具与能力，决定前后对照原指针和历史。 |
| 状态 | 已修复，定向原树/修树及最终主线记录可交叉核对。 |
| 复核 | [原树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201932268/job/131127269538) · [修复树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201932268/job/131127269303) · [开发与主线补丁映射](REMEDIATION.md) |

## P57-SEC-03 · P1 · S3 / 终审身份独立性

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/creative_review.py`，432–443，463–477，`CreativeReviewService.decide` |
| 相关来源 | `src/novel_factory/application/review_independence.py:53–71，160–191` · `src/novel_factory/application/editorial.py:501–523` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/creative_review.py#L432](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_review.py#L432) |
| 描述 | 正式终审仅比较实际控制器与原作者、审稿人的 principal_id，独立性收据验证作者与读者，未将终审者的实质主体组纳入原签名。 |
| 触发条件 | 一份合格的阅读和合法完整的原生终审申请有多个 NF 身份，而当前控制器没有独立的编辑主体组凭据。 |
| 复现及观测 | 让已取得授权的控制器对自有合成稿使用正规 RETAIN 决定，并用独立登记过和未登记的编辑身份做对照，保存原生决定状态。 |
| 实际影响 | 在多身份环境中，原终审可被接受而无法证明终审者与作者、审稿人实质分离。未观察到对既有世界事实的改变。 |
| 修复建议 | 把当前控制器 grant 和 epoch 绑定到登记的实质主体，逐一比对同卷相关稿件的作者谱系与独立审稿人，并保留正式审批证据。 |
| 状态 | 已修复，定向原树/修树及最终主线记录可交叉核对。 |
| 复核 | [原树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202380763/job/131128823797) · [修复树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324597/job/131132081080) · [开发与主线补丁映射](REMEDIATION.md) |

## P57-CR-01 · P1 · S5 / A/B 留出资格

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/adaptive_evaluation.py`，325–396，799–842，`_quality / AdaptiveEvaluationService.inspect / require_release` |
| 相关来源 | `src/novel_factory/application/adaptive_instructions.py:498，1008–1029` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/adaptive_evaluation.py#L325](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/adaptive_evaluation.py#L325) |
| 描述 | 工程晋级门槛按报告 revision 计算正负留出样本。底稿和有重叠的多稿阅读窗口没有独立计数。 |
| 触发条件 | 两个已签名的读者结果同源，一份为 PROSE 正例、一份为 NAMING 保留负例。两份报告都与训练集分离。 |
| 复现及观测 | 通过 S3 对同一留出稿提交不同层的解读，再从 S5 正常提案、冻结、运行和尝试取晋级资格，保留全部配对测量。 |
| 实际影响 | 原实现以一篇稿件提供两个独立控制并满足工程晋级资格。正式指令发布仍需另行审批。 |
| 修复建议 | 用全部冻结来源正文的内容 SHA 检查实质重叠，重复稿不具备送审资格。旧报告保留原始有签名的数据，并在使用时按当前规则核验。 |
| 状态 | 已修复，定向原树/修树及最终主线记录可交叉核对。 |
| 复核 | [原树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43199663974/job/131119434366) · [修复树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43199663974/job/131119434962) · [开发与主线补丁映射](REMEDIATION.md) |

## P57-TC-01 · P1 · 主线 Workshop 集成验收

| 字段 | 内容 |
| --- | --- |
| 定位 | `.github/workflows/agent-workshop.yml`，90–93，267–280，`Verify dual-ledger recovery on the integrated NF source and native PG17` |
| 相关来源 | `.github/workflows/creative-harness.yml:137–139` · `workshop/integration/` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 .github/workflows/agent-workshop.yml#L90](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/.github/workflows/agent-workshop.yml#L90) |
| 描述 | 常规 Workshop 工作流用根解释器运行整个集成套件，Workshop 安装于另一虚拟环境，隔离子进程找不到包或 aw-mcp 命令。 |
| 触发条件 | 主线推送后，用干净的官方运行器走原 Workshop 集成工作流。 |
| 复现及观测 | 保留原主线作业的九项失败，在完全相同的工作流里安装检出的 Workshop 分发到根解释器并把该解释器的脚本目录加入本步骤 PATH，复跑整个集合。 |
| 实际影响 | 正常主线载体无法给出九项真实双库测试的结论。当时另一个专用载体 11 项通过。 |
| 修复建议 | 在实际启动子服务的解释器安装当前 checkout 的 Workshop，保留独立环境、服务 UID 与实数据库验收。 |
| 状态 | 已修复，定向原树/修树及最终主线记录可交叉核对。 |
| 复核 | [原树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896850/job/131113456793) · [修复树](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201789218/job/131126778921) · [开发与主线补丁映射](REMEDIATION.md) |

## P57-CR-02 · P2 · S3 / 文学读者意见

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/creative_review.py`，310–329，`CreativeReviewService.review` |
| 相关来源 | `src/novel_factory/domain/creative_review.py:127–207，325` · `tests/unit/test_creative_review_rubrics.py:36–61` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/creative_review.py#L310](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_review.py#L310) |
| 描述 | PLOT 和 STAGING 没有自动判断路径，空 observations 既可代表读者审清，也可代表读者未表态。两者入库后均可得到 PASS/ACCEPT_CANDIDATE。 |
| 触发条件 | 合格读者取得完整阅读回执，并在这两个层之一不填写意见。 |
| 复现及观测 | 在原合并及最终修复产品代码上，合格独立读者在真实 PostgreSQL 绑定稿件和阅读覆盖，分别运行 PLOT / STAGING，刻意留空 observations。两层均写入 native PASS 和 advisory ACCEPT_CANDIDATE，各树两项通过。记录本身没有明示已审清的字段。 |
| 实际影响 | 下游可能过度解读空读者意见，正式世界接受仍有独立流程，读者审清也确属合法业务。 |
| 修复建议 | 给人工或受托读者增加明示已审清/未给判断的区别，补两种业务结果的成对用例。 |
| 状态 | 开放、已评估延期。需要约定读者与编辑端口的语义并兼容历史报告。当前部署保留逐稿人工阅读。 |
| 复核 | 原合并和修复树上均各 2 passed。[双版本真实读稿观测](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43205583624) |

## DB-01 · P2 · 双系统物理恢复资格

| 字段 | 内容 |
| --- | --- |
| 定位 | `workshop/integration/test_paired_restore.py`，95–139，250–285，389–399，476–519，`test_physical_pitr_both_directions` |
| 相关来源 | `reports/harness/S6_ACCEPTANCE.md:12` · `workshop/integration/conftest.py:18–79` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 workshop/integration/test_paired_restore.py#L95](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/workshop/integration/test_paired_restore.py#L95) |
| 描述 | paired-restore 在一个 PostgreSQL 物理集群创建两个服务库，基于一份基备与一条 WAL 时间线恢复后交叉搭配 live peer。S6 验收文档对独立物理备份的表述过宽。 |
| 触发条件 | 两个真实独立集群分别归档，恢复点与时间线出现偏差。 |
| 复现及观测 | 核对 initdb、pg_basebackup、服务 DSN 和恢复点，主线实际 PITR 日志及双向错配测试验证已有范围。两个独立归档的异步恢复没有执行。 |
| 实际影响 | 尚无对两条独立 WAL 时间线恢复点偏移的实测保证，现有测试未观察到数据丢失。 |
| 修复建议 | 按两独立集群生成两份基备及归档，故意错开命名时间点并保留两个 system_identifier、LSN 和对账结果，更新正式验收用语。 |
| 状态 | 开放、已评估延期。需单独组织双集群灾备资格演练，不能以已通过的单时间线试验承诺现场灾备。 |
| 复核 | 代码与实际恢复收据交叉确认，独立双归档未测 |

## PERF-01 · P2 · S4 / 动态实例历史

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/dynamic_agents.py`，57–66，164–174，628–668，`DynamicAgentService.history` |
| 相关来源 | `src/novel_factory/infrastructure/objects.py` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/dynamic_agents.py#L57](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/dynamic_agents.py#L57) |
| 描述 | 历史接口按实例遍历全部事件和修订，逐项做 SQL、对象校验及内容指纹校验，且在该期间持有作品共享锁。 |
| 触发条件 | 控制员读取长期活动、已有大量版本与事件的员工实例。 |
| 复现及观测 | 按实际源码和现有 5 个事件、2 个修订的场景计算，单逐项循环已有 7 次 SQL 与 21 次 fsync 调用，不对该计数作壁钟延迟换算。 |
| 实际影响 | 查询、响应大小与长历史持锁随历史线性增长。未给出生产 P95、死锁和用户受影响次数的实测值。 |
| 修复建议 | 设计带冻结 epoch 的分页游标及批量检索，保留逐项完整性语义，另做长历史基准测试。 |
| 状态 | 开放、已评估延期。分页需要兼容返回契约和历史完整性验证。影响已授权的管理查询，未发现对核心世界或费用账本的破坏。 |
| 复核 | 源码计数与现有示例，未运行长历史性能样本 |

## PERF-02 · P2 · S6 / 角色两阶段调用计量

| 字段 | 内容 |
| --- | --- |
| 定位 | `src/novel_factory/application/creative_metrics.py`，61–77，190–212，382–409，`_spend / CreativeMetricsService.report` |
| 相关来源 | `src/novel_factory/application/actor_lookup.py:105–146` |
| 被审核代码 | `1de471076b8fb4240f95ff8033c60db34c187404` · [源码 src/novel_factory/application/creative_metrics.py#L61](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L61) |
| 描述 | S6 投影只从 AGENT_CALL_EVIDENCE 归集调用，漏归第一阶段 AGENT_ROLE_TOOL_RESPONSE。 |
| 触发条件 | 角色以正常工具选择完成模型第一阶段，特别是有 provider token 而金额 UNKNOWN 的响应。 |
| 复现及观测 | 在原始合并、真实 PG 和本地隔离的角色回放上跑 read 与 unknown-first，再用同一 controller 查询真实分支 S6 report。 |
| 实际影响 | 成功两调用共 58 Token 和 0.10 夹具金额，其中一笔 29 Token 被列为 UNCLASSIFIED_LEDGER，第一调用 UNKNOWN 时 provider 返回 29，而报表 known_tokens=0、实际金额 null。权威 Attempt、预留及防重发未受影响。 |
| 修复建议 | 以授权工作、分支、任务代际与 attempt 连接首阶段不可变回执，金额未知保持 null，已知 Token 单独记录，核对 Skill、调用数量及延迟分组。 |
| 状态 | 开放、已评估延期。属于观测分类，金额与预算的权威来源独立。投产计量前须修正并回填相容范围，本次只选取严重修复进入主线。 |
| 复核 | [原树真实 S6 报表](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202106191/job/131127864815) |
