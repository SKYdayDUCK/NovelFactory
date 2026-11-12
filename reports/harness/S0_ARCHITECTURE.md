# NovelFactory Creative Harness 技术架构 v0.1

S0 的源码基线是 `276820316129d546b0a13118bcbe283b06507795`。其承载的业务代码已经由 #16 验证。S0 固定既有系统的入口、待实现的数据流、权限边界和验收口径。下表的 S1–S6 栏表示后续实现责任。本阶段可执行的交付是契约、正反例和加载审计。[第一阶段签收](https://github.com/SKYdayDUCK/NovelFactory/blob/1b0302a6867c56f5871c0b570477d88dc1ffe0a7/reports/integration/PRE_HARNESS_BASELINE.md)

## 权威数据和服务边界

| 层 | 当前入口及持久化责任 | Harness 的使用方式 |
| --- | --- | --- |
| NovelFactory Core | `WorldQueryService.context_compile`、`RuntimeTaskService`、`AgentGateway`、`ReleaseService`。NF PostgreSQL 保存世界、故事时间、任务代际、访问能力、审批与外部调用账本。对象存储按内容寻址。 | 在已有 principal、task、generation 和 WorldRef 内工作。返回有哈希的候选或审核证据。正式接受、冻结正文与发布仍走各自的 NF 服务。 |
| Agent Workshop | `organization.py` 维护雇佣状态和有效范围，`delegation.py` 与 NF 用实际 MCP 回执关联。`reviews_skills.py` 管理授权、Skill 冻结、审核独立性和有限返工。Workshop PostgreSQL 维护组织，Redis 承担可恢复通知。 | 使用受限委托及岗位作用域分配任务。正文留在 NF，只在组织侧传递已授权引用、指纹、席位和结果摘要。 |
| Creative Harness | S1–S6 在应用层组合上述服务。缓存、度量和候选均从指定原件派生。 | 新的持久数据限于版本化指令、候选、审读标注、评估和发布依据。以主键或哈希关联 NF 的现有世界/任务/Attempt 与 Workshop 身份。 |

从作者或经理发起的业务路径从现有 CLI/MCP 的服务端身份和授权范围开始。Worker 领取指定代任务。按 task 绑定编译上下文并取必要素材。在发送前验证授权与额度。模型或工程夹具返回候选。独立审核并向对应接受入口提交。模型等待发生在事务外。丢失回执用原业务键查询。后台队列通知可重发，数据库保存的代际、回执、占用额和产物决定重放结果。

## 逐阶段设计与已有落点

| 能力 | 已存在的机制 | 需要补足的决策和出口 |
| --- | --- | --- |
| H1 / S1 上下文 | `RuntimeCatalog.snapshot` 读取一个角色和一个已选主 Skill 并冻结原文。网关在 `_messages` 内分别读取正式上下文、连续性、编辑及协作材料。演员查询限定在固定故事时间及其 BELIEF。 | 把面向模型的服务协议与供正文使用的事实、人物声音、作品风格、章节接缝分开。记录每项授权来源、版本、实际加载或省略及原因。规范化字节计算摘要，任务绑定在派发前复验，恢复时重用原快照。 |
| H2 / S2 候选规划 | `PlanningService` 支持条件计划、独立窗口审查和采用。`RollingPlanning` 有参考线、变更请求和局部重规划边界。 | 符合已有计划的普通场景可走单路径。动机冲突或关键前提改变时在同一冻结起点生成有限候选，记录角色目标、可观察阻力、因果依据、局部修改范围、反证和未决项。允许弃用全部候选。候选采用只产生计划关系。 |
| H3 / S2 隔离推演 | `WorldSchedulerService` 在共同的世界切点登记互动、密封行动、选择结果。现有世界分支和冲突检测保存历史。 | 候选只引用指定 WorldRef 和假设空间，同一角色各路径只见自己的 BELIEF。比较后果并报告停止原因。候选数、累计 Token 和时间均有硬上限。发起额外模型调用仍经 NF 预留。要改变正式世界，重新走已有裁决和接受权限并复验当前版本。 |
| H4 / S3 审核与修订 | NF `ReviewService` 已能读取有覆盖凭证的正文，登记审核、编辑决定和修订。Workshop 在雇员和生产组两个维度判定利益冲突。 | 问题绑定确切版本、文本范围和引文摘要，记录维度、判断理由、影响、反解释、建议、最小修订范围。新稿使旧锚点失效，复核要绑定新内容。保留作者异议、撤回和重复返工。正文接受与世界接受分别走原服务。 |
| H5 / S4 可选动态岗位及 Skill | Workshop 可登记有版本的来源、共享、解析、冻结、撤权，NF 有真实角色与工具权限。 | 解析已授权的岗位模式、Profile 和指定 Skill。按作品/团队/角色选版本，服务端实际许可取交集。任务启动冻结内容与身份。派发、读取、交付仍检查当前权限代际。退出、归档或切回旧指针均保留历史。 |
| H6 / S5 候选指令优化 | `RuntimeUpgradeService` 的世界语义升级展示了如何绑定审批人与原件。其 `activate` 会推进世界 HEAD 和解释代际。`ChapterCostService` 支持按原账本计量。 | 将冻结失败样本关联到上游世界设计、编剧、上下文装配、写作者或漏检审核等假设。对候选做离线 A/B 并保留所有失败。由获准发布者选择受评版本，仅影响新的任务绑定。纯指令发布和回滚只写该作用域的指令版本选择与审计回执，保留正式世界 HEAD、解释代际和事件流。 |
| S6 观测与最终对照 | `LogService` 的工具、模型、产物、场景级 trace 和 Workshop 对账已经接入服务端。 | 使用现有审计事件/费用引用和受权查询汇总。分别报告原静态、Harness 固定指令、动态配置与候选优化均实际开启的 Harness。第三组保存该轮触发、生成、评估、采纳或拒绝的回执和成本。在相同输入和预算下比较资源和工程结果。 |

## 调用、存储和故障语义

- **上下文与产物绑定。** 原 task/generation、context_bundle_id、WorldRef、知识切面、角色和每个来源的 immutable revision 一起决定快照。摘要可以证明同一内容，取得原文仍需本人范围下的授权读取。没有可用的风格、接缝或视角原件时记录缺口，不把未知状态合成为世界事实。受限角色不会接收到旁观者材料或规划底稿。
- **查写与调用。** 生成候选、审核问题和评估不产生世界接受或稿件批准。带副作用的操作要求单作品/分支的可信身份、代际、原幂等键及预期当前版本。并发签发与重试得到同一原收据或明确冲突。派发前原子预留，UNKNOWN 和未判定发送继续占用，撤权阻断结果交付并保留审计证据。
- **审核与反馈。** 分开记录世界设定、概念命名、编剧、调度、角色对白、句段、接缝、卷级结构和事实/认知一致性。同一段“账”可有必要的交易功能，也可能被所有人物重复当作关系隐喻。审读要给出局部证据和合理反解释，允许无问题、弃权和无需改动。锚点坐标、评审身份、修改版本和复核状态见[创作评估协议](S0_EVALUATION.md)。
- **动态与回滚。** 配置层次为全局 `creative_harness.enabled`、独立子项 `dynamic_agents.enabled` 与 `adaptive_instructions.enabled`，后两项位于 `creative_harness` 下。自动晋级选项 `creative_harness.adaptive_instructions.auto_promote` 也默认关闭。所有开关均为假时，旧任务、既有静态模型调用与工具目录按原入口执行。旧记录缺新的可选绑定时进入相同静态语义。登记候选、作者授权调用真实模型、批准发布是三个独立动作。
- **持久性。** 大正文和评估原件交由已有对象存储，记录内容摘要及来源。不把正文、私有提示词、个人审读表或凭据写进公用日志和 Workshop。跨库恢复分别校对 NF 原消费/任务与 Workshop 原委托。先按同一 operation/attempt 查回执，未知占用留存，撤权记录的恢复水位进入核对。确认一致前暂停相应交付。

## 实现和验收入口

S0 的候选 Schema 位于 [Creative Harness 契约目录](../../contracts/creative_harness/creative_harness.schema.json)，检查器使用本地已冻结的 Core 类型。后续阶段在需要发布具体接口时登记对应的生产版本。契约字段、外部类型与不适合用 JSON Schema 表达的检查见[接口与数据模型](../../contracts/CREATIVE_HARNESS.md)。逐文件来源和加载差异见[指令审计](S0_INSTRUCTION_AUDIT.md)。后续阶段逐项引用[验收矩阵](S0_ACCEPTANCE_MATRIX.md)，遇到授权、身份、切面、预算或恢复回归时先在当前阶段修复，再按相同门槛重跑。

基线源码包含 [RuntimeCatalog](../../src/novel_factory/infrastructure/runtime_catalog.py)、[上下文编译](../../src/novel_factory/application/queries.py)、[AgentGateway](../../src/novel_factory/application/agent_gateway.py)、[受限 Host](../../src/novel_factory/interfaces/role_context_host.py)、[编辑审批](../../src/novel_factory/application/editorial.py)、[Workshop 授权与审核](../../workshop/src/agent_workshop/reviews_skills.py)、[运行时升级](../../src/novel_factory/application/upgrades.py)与[场景级审计](../../src/novel_factory/application/audit_log.py)。
