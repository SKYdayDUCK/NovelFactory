# NovelFactory 全量开发接管与续办工单

本工单归档委托人原始十三节要求。文中的既有 SHA 和 PR 状态属于接管时给出的历史参考。恢复执行时须重新查询 GitHub、检查工作区，以实际代码、数据库、日志、测试、PR 和最终主线提交签收每个阶段。[仓库](https://github.com/SKYdayDUCK/NovelFactory) · [主线](https://github.com/SKYdayDUCK/NovelFactory/tree/main) · [全部 PR](https://github.com/SKYdayDUCK/NovelFactory/pulls?q=is%3Apr) · [开放 Issues](https://github.com/SKYdayDUCK/NovelFactory/issues)

## 一、目标与严格串行制度

实际完成开发、验证、GitHub 提交与正式合并。目标包括将 P5.6 剩余功能按阶段集成进 main，收尾并正式合并 P6 现有工程能力和证据，保留真实本地创作的独立验收边界，以及实现 P5.7 细粒度创作审核与可选动态 Agent/Skills 系统。最终开放 PR 清单只保留 Scion #4。其他存量及本轮新建的开发 PR 均须正常合并或有依据地关闭；需持续跟踪的 Issues 可保持开放。[Scion #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)

每个阶段都须走完「开发 → 测试 → PR → CI → 正式合并 main → 在合并后的 main 重新验证 → 阶段签收」。上一阶段进入 `MERGED_AND_VERIFIED` 后方可开启下一阶段开发 PR。历史堆叠 PR 可以保留分支和证据，必须按依赖顺序逐个整理、验证及集成。禁止并行推进不同阶段的功能开发，禁止将未经独立验收的阶段混合合并。

## 二、恢复现场与确认真实进度

委托人给出的历史起点为 main `d039839fe68f6403ea6e5590af3ed787318d5b5f`、P5.5 #6、Workshop W0 #7 和 W1 #8 已合并，W2 #9 最近候选为 `162af2695b76`、当时待合并。后续历史 PR 是 NF 桥接 #11、W3 #10、W4 #12、W5 #14、W6 #15、P6 预检与部署 #16、P6 全局 Token 预算 #17、P5.5 额外证据 #18；Scion #4 保持独立。[历史 main](https://github.com/SKYdayDUCK/NovelFactory/commit/d039839fe68f6403ea6e5590af3ed787318d5b5f) · [P5.5 #6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) · [W0 #7](https://github.com/SKYdayDUCK/NovelFactory/pull/7) · [W1 #8](https://github.com/SKYdayDUCK/NovelFactory/pull/8)

- 启动时重新核对全部 Git 分支、远端提交、worktree、未提交代码、stash、reflog 和其他 Agent 可能遗留的未提交成果。
- 逐 PR 核对最新 SHA、base、mergeability、Draft、评论和 CI，检查 `reports/integration/` 现有台账及验收报告，辨认已完成代码与后续分支修复是否重复。
- 优先恢复和保护现有代码。禁止未经确认执行 `reset --hard`、`clean`、force push、删除工作区或覆盖其他 Agent 成果。不得仅依旧交接报告认定完成。

## 三、任务 A：完成 P5.6 剩余集成

### W2 及依赖顺序

原始执行起点为继续 PR #9。复查当前候选、历史失败、最新 CI 及实际阻断，复用已有 W2。每项失败须辨别测试实现错误、环境故障和业务回归，只修复实际阻断。

W2 专项验收包含双向等级通信、合法路由与越级拒绝，发送人、接收人和安装归属授权，临时路由次数、撤权与过期，Redis Streams/PEL/ACK，PostgreSQL inbox/outbox 与消息幂等，Redis 丢失、重建、重放及服务重启，W0/W1 已部署实例与旧数据库迁移，两套真实 MCP 服务、两套 PostgreSQL 身份与必要权限隔离。正式合并后在 main 重跑适用验收。

| 顺序 | 阶段 / PR | 原始验收范围 |
|---|---|---|
| 1 | [Workshop W2 #9](https://github.com/SKYdayDUCK/NovelFactory/pull/9) | 消息、授权、持久化、恢复、存量兼容与双服务 |
| 2 | [NF 受限委托桥接 #11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) | 委托授权、幂等、任务与 Attempt 账本、预算预留、UNKNOWN、丢回执和双数据库恢复 |
| 3 | [Workshop W3 #10](https://github.com/SKYdayDUCK/NovelFactory/pull/10) | 按阶段范围交付，并验证 W3–W6 的相应能力 |
| 4 | [Workshop W4 #12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) | 同上 |
| 5 | [Workshop W5 #14](https://github.com/SKYdayDUCK/NovelFactory/pull/14) | 同上 |
| 6 | [Workshop W6 #15](https://github.com/SKYdayDUCK/NovelFactory/pull/15) | 同上 |

W3–W6 继续核验岗位与 Skills 共享、独立专业审核、管理者恢复、工作状态、跨服务协作及并发员工容量。每个堆叠阶段轮到时以最新 main 为目标，只保留本阶段应有差异，去重已进入 main 的共同祖先和补丁。保留历史提交、测试证据和审查记录，必要时采用干净集成分支；新装与已发布用户的数据库升级路径均须验证。

### W6 后的 P5.6 主线全系统验收

1. 两套服务的启动、安装、MCP 调用和独立撤权。
2. 完整组织图、层级沟通、雇佣、调任、员工退出。
3. 多角色任务委托、业务回执与成本对账。
4. 角色 BELIEF 隔离、信息披露权限。
5. Skills 的共享范围、版本、撤销和职责分离。
6. 创作审核与编辑审核既有流程。
7. Redis 中断、网络丢回执、两库不一致及恢复。
8. 故事冲突与定向修复流程。
9. 10/50/200 员工专项及历史 NF 容量测试。
10. 合并后真实工程回归、迁移和 PITR。

在最新 main 形成 P5.6 最终签收记录，随后进入 P6。保留历史容量未达标原始数据、原阈值和 Issue #13；不得伪造通过。[容量 Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)

## 四、任务 B：P6 现有工程成果与证据收尾

P6 本轮首先交付已经开发的工程准备与测试证据。逐项为下列结论记录精确代码 SHA、原始数据、执行环境和结果。

- 离线实验配置和模型环境检查，作品、分支、初始 WorldRef、计划、角色绑定。
- 真实 PostgreSQL 预算与迁移，全局 Token 限额及单次调用限额，并发预留总额受授权预算约束。
- UNKNOWN Attempt 保留费用和 Token 占用，物理 WAL/PITR 后账本一致。
- Workshop/NF 双服务测试、模拟场景、正式产物引用和审核回执、运行手册及可重现入口。

保留六个场景的确定性工程样卷《潮闸余灯》及全部历史证据，路径为 `evidence/p2/batch3/short-volume/run/short_volume.md`，分类为 `TEST_FIXTURE`。P6 报告须分别记录确定性工程正文、模拟供应商生成、真实模型创作、独立文学审核及作者最终验收。禁止将样卷记为商业模型作品或文学质量证明。它可以作为 P5.7 的负面案例，不能单独作为全部文学质量标准。

P5.6 完整签收后，先集成 #17 共享预算，再集成 #16 离线实验预检、部署、发布候选、运行入口与证据。按精确依赖整理到最新 main，每个 PR 独立通过适用测试、合并，再在 main 复核。归档 P6 模拟测试和运行原件，交付完整的本地真实创作交接文件。[预算 #17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) · [预检与部署 #16](https://github.com/SKYdayDUCK/NovelFactory/pull/16)

作者计划在自己的 Windows/WSL2 本地环境完成真实 P6 实验。原委托未提供正式实验所需的私有作品、模型绑定、消费预算和独立读者。缺少真实模型授权时记录 `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。禁止开发环境擅自发送商业模型请求，禁止宣称已在作者本机完成；该边界不能用于永久搁置已满足工程发布标准的工程 PR。

交付的部署、运行与验收材料须支持作者使用真实模型连续生成 10–20 个场景，查看全文及各版本修改、角色行动和世界状态变化、各 Agent 的生成与审核记录，统计真实 Token、成本、耗时和返工，并开展作者与独立文学评价。

## 五、P5.7 总体目标

正式名称为 **NovelFactory P5.7 — Fine-Grained Creative Review & Adaptive Agent/Skills**，中文为**细粒度创作审核与自适应 Agent/Skills 系统**。同时处理文笔问题和上游导致小说明显 AI 味的问题：

1. 世界设定过度模式化。
2. 人物与组织命名缺乏自然性。
3. 大量没有剧情必要的象征性概念。
4. 将工程机制直接写入小说正文。
5. 过度解释人物心理和剧情意义。
6. 缺乏真实行动、互动与对话。
7. 人物声音趋同。
8. 审核 Agent 粒度过粗，定位不准。
9. 审核反馈无法归因到真正的问题 Agent。
10. 角色指令和 Skills 缺少有效的持续改进机制。

分别建模和验证设定自然度、叙事可信度、人物差异、文笔质量和工程一致性。不得靠删词或单一「AI 味」评分决定质量。

## 六、P5.7 第一项：审计现有 Agent 与 Skills 指令体系

先完整审查既有配置、职责和加载路径，保留可用成果。逐文件、逐角色生成审计矩阵，至少覆盖：

| 位置 | 重点文件 |
|---|---|
| 工程规范与索引 | `AGENTS.md`、`SKILLS.md` |
| 运行时角色及目录 | `runtime/roles/README.md`、`runtime/roles/catalog.json`、`runtime/roles/world_designer.md`、`runtime/roles/volume_director.md`、`runtime/roles/character_actor.md`、`runtime/roles/writer.md`、`runtime/roles/reviewer.md`、`runtime/roles/literary_editor.md`、`runtime/roles/chief_editor.md` |
| 运行时 Skill 目录 | `runtime/skills/catalog.json` |
| 运行时 Skill 指令 | `runtime/skills/prepare-world/SKILL.md`、`runtime/skills/plan-volume/SKILL.md`、`runtime/skills/write-scene/SKILL.md`、`runtime/skills/review-consistency/SKILL.md`、`runtime/skills/edit-volume/SKILL.md`、`runtime/skills/research-literature/SKILL.md` |

核对实际任务上下文组装、Agent Host、Skill 选择、版本冻结与 Prompt 加载。根目录 `AGENTS.md` 管开发工程规范，`SKILLS.md` 是索引。小说运行时指令位于角色、Skills 和任务上下文里；每项指令进入目标 Agent Context 都要有运行时加载证据。

每种角色和 Skill 都须审查职责准确性、工程正确性是否挤压创作、文学方法能否执行、抽象概念依赖、指令冲突、正反例是否恰当、模板化命名/组织/事件、世界状态 Schema 对正文的污染、反复解释人物动机和意义、作者风格/作品风格/人物声线/系统规则的区分、审核反馈与修订路径，以及跨角色重复 Skill 和 Prompt 膨胀。给出逐项事实，不能只有笼统的优化判断。

## 七、P5.7 第二项：细粒度创作审核

兼容扩展 P5.6 的编剧、导演、角色、文笔、世界一致性、系统稳定性及统计审核。

| 审核范围 | 必须覆盖 |
|---|---|
| 设定级 | 世界规则、命名、人物设定、组织结构、概念重复、题材适配 |
| 剧情级 | 动机、因果、冲突、悬念、故事目标、设定与情节的相关性 |
| 场景级 | 行为、互动、节奏、对白、情绪推进、叙事视角 |
| 句段级 | 空泛议论、机械总结、过度解释、模式化比喻、重复修辞、不自然用词、工程语言泄漏 |
| 章节级 | 风格连贯、重复表达、事件密度、人物主动性、场景接缝 |
| 整卷级 | 人物成长、长期冲突、主题表达、文风漂移、跨章同质化、读者体验 |

专项 AI 味和概念自然度审核须逐处分析题材与世界事实依据、实际剧情功能、人物自然感知/表达、机械隐喻重复、删换是否损伤故事、作者明确保留的文学意图。提供原文证据和反解释，避免误删。禁止全局禁词表；「账」可服务财贸、记账剧情，「名字」可服务身份与传承，「印章」可以是推动事件的实物。关注无端反复强调的主题象征。

《潮闸余灯》负例覆盖知识隔离规则直接入旁白、计划与依赖失效语言混入叙事、系统式联合裁决替代人物行为、反复向读者解释象征意义、角色关系和情绪被压缩成抽象总结。

ReviewIssue 契约至少包含以下信息：

- `review_id`、`reviewer`、`review_mode`，原始文本 ArtifactRef 与内容 SHA。
- 精确位置、原文引文、问题类型、严重级别、创作层级、与作品目标的具体冲突。
- 具体证据、反解释、推荐处理方式、最小允许修订范围。
- 可能的上游原因、关联角色指令或 Skill 版本、修改前后对照、复核结果、最终审核或编辑决定。

写作者不得自行完成最终独立审核，审核任务不得给审核员额外角色秘密。区分硬一致性问题与审美建议；审核覆盖可核对，每个问题有限返工，失败可沿修订和复核路径追踪。修订须保护已接受的世界事实。每次修改与审核保存版本与预算记录。模糊评分不能强制改正文，编辑可在审美分歧下保留原文。

## 八、P5.7 第三项：可选动态 Agent 与 Skills

提供明确总开关，建议 `dynamic_agents.enabled = false`，默认关闭。允许经受信配置或授权管理接口启用。关闭时使用原静态角色和 Skills、组织和权限；不动态生成角色、不进行自动 Prompt 优化、不自动更新运行指令、不产生优化模型 Token。实测现有任务、MCP 接口、世界管理和审核流程的兼容性。

开启后的范围为按既有岗位模板创建实例，按任务和组织需要配置工作模式，从允许集合选择组合 Skills，维护作品/团队/角色级指令版本，分析审核历史及失败样例，提议角色 Prompt 与 Skill 修订，在冻结样本上 A/B 验证，保留有效候选、拒绝无收益修改，经授权发布，质量回退时恢复旧版本。

继承 P5.6 组织、员工、Skills 共享和 NF Grant；有效权限由服务端判定。动态角色必须有来源模板、实例身份、所属组织、有效任期、权限范围和撤销语义。新的创作偏好不能带来新服务权限。动态 Agent/Skills 不得：

1. 自增工具权限、绕过等级通信规则或自行签发 ADMIN。
2. 访问其他角色未授权 BELIEF、修改世界真相或正式历史。
3. 绕过任务预算、改审核结果来批准自身更新，或借 Skill 文本提升系统权限。
4. 将外部网页、小说正文或工具返回的指令提升为可信配置。
5. 撤权后继续用旧实例扩大能力。

按已有模型实现必要结构。原要求列举 `AgentProfileRevision`、`SkillRevision`、`DynamicAgentInstance`、`PromptCandidate`、`SkillCandidate`、`EvaluationRun`、`PromotionDecision`、`RollbackRecord` 作为示例。发布版本须保存内容哈希、来源、依赖版本、生效范围、批准记录和可复现引用。运行中的任务继续用冻结的合法指令快照，授权撤销实时生效。升级须使用明确的新任务代际或相容版本切换，禁止静默改动运行中任务的指令。

自动优化按「收集真实审核问题 → 问题归因 → 生成候选 → 冻结评测 → 对照比较 → 审批或受控晋级 → 发布 → 持续监控 → 必要时回滚」。归因分别考虑世界设定 Agent、编剧或卷纲、场景写作、审核者、上下文材料或任务组装、模型能力限制，不能一律归因至 writer。初期可自动产生候选并测试，生产发布受授权策略约束，禁止模型无限改写核心约束。

## 九、P5.7 第四项：日志、指标与可观测性

接入 NovelFactory 和 Workshop 的既有日志、统计和事件体系，避免另一套不兼容的系统。

### 结构化日志

至少记录 work_id、branch_id、WorldRef、task_id、generation、attempt_id、员工或 Agent 实例、role_id、role mode、SkillRevision 及哈希、PromptRevision 及哈希、动态开关状态、模型配置版本、审核任务与问题类型、审核覆盖范围、修改前后产物引用、优化候选/评估/批准/拒绝/回滚、Token/费用/耗时/重试结果，以及失败/超时/撤权/恢复状态。

保护私有正文、密钥及未授权的角色认知，防止跨租户、跨员工泄漏。大段原文通过受控 Artifact 的合法引用关联，不能在日志中无限复制全文。

### 工程指标

至少统计各审核层级运行次数和覆盖率、各类审核问题发生率、修复率、复审通过率、错误归因及人工推翻率、审核平均与 P95 耗时、每项审核任务 Token 与费用、每章/每卷创作与审核成本占比、动态实例创建/退出/撤销次数、Skill 选择/使用/更新次数、优化候选通过率、发布指令回滚次数、开关前后延迟与成本差异、运行时版本不匹配与失效任务数。

### 文学质量指标

分维度评价设定自然度、角色与组织命名合理性、场景演绎程度、对白自然度、人物声线差异、情节因果可信度、过度解释与重复表达、无必要概念与象征意象密度、章节可读性、整卷连贯性。可稳定自动计算的指标可自动统计；主观指标须记录评价者、评估协议、样本范围和不确定性。禁止合并成唯一 AI 味分数。没有真实盲评不能宣称文学质量提升。

### 基线与对照

在相同冻结剧情输入、世界版本、模型配置和可比预算下比较四组：

1. 旧静态指令 + 旧审核。
2. 新静态指令 + 细粒度审核。
3. 动态 Agent 开启，指令不自动晋级。
4. 动态 Agent 开启 + 候选优化机制。

报告质量、Token、费用、耗时和故障差异，保留失败样本。

## 十、P5.7 按 S0–S5 独立开发、合并与复验

禁止一个巨大 PR 合并全部 P5.7。各阶段可根据实际依赖合理调整边界，但必须逐阶段开发、测试、PR、CI、合并、main 复验并签收。

| 阶段 | 实际交付及独立验收 |
|---|---|
| S0 需求审计与详细架构 | 交付现有 Agent/Skills 逐文件逐角色审计矩阵，细粒度审核需求与详细设计，可选动态 Agent/Skills 架构，数据模型与事件流，特性开关、权限、回滚设计，指标与日志方案，以及完整验收矩阵。核查实际任务上下文和加载路径并做必要可行性验证，作为独立阶段合并 main。 |
| S1 细粒度审核内核 | 实现审核层级、ReviewIssue 契约、路由、阅读覆盖、证据定位、职责分离、有限返工和复核。完成真实数据库和服务集成测试后合并 main。 |
| S2 Agent/Skills 指令重构 | 基于 S0 审计改进世界设计、编剧、写作、审核与编辑的实际运行时指令。新增有意义的正反例和测试材料，证明指令实际进入运行时 Context。保留旧版本供回滚，独立验收后合并 main。 |
| S3 动态 Agent/Skills 基础 | 实现默认关闭总开关、动态实例、版本化角色配置、Skill 选择与共享、实例生命周期和授权边界。验证关闭、开启、运行中撤权、崩溃恢复、升级和回滚后合并 main。 |
| S4 反馈驱动的指令优化 | 实现失败收集、跨 Agent 问题归因、候选生成、冻结评测、A/B 比较、受控晋级与版本回滚。无真实供应商授权时，可做离线工程验证，文学提升须标为待真实评估；测试后合并 main。 |
| S5 全系统集成与可观测性 | 接入既有日志、统计、预算、审计。完成开关双模式、回归与负载对照，运行必要的双服务、PostgreSQL、Redis、MCP、故障恢复与权限隔离检查。形成完整工程验收报告，修复阻断后合并 main。 |

## 十一、质量与安全验收

保留统一世界时间线、历史不可变、WorldRef、BELIEF 隔离、KnowledgeCut、角色授权代际、事件正式接受、任务幂等、预算和 UNKNOWN、双服务独立所有权。

文学审核须有代表性正反例，合法隐喻、不可靠叙述、人物误信和题材特征不能简单判错。要求指出具体文本为何有问题，不能只输出模板化评价。

动态开关关闭后静态系统能独立运行。开启后须防止权限扩张、历史污染、任务版本漂移、跨人物信息泄漏、自审自批及不可控自动优化循环。

针对细粒度审核 Token 开销，评估确定性检查优先、按风险触发层级、句段抽样与增量复审、文本哈希与证据缓存、仅修改受影响区域、已通过问题不重复审核、预算耗尽暂停交接。实际测量成本与收益，不能默认让所有 Agent 对每个场景执行全部审核。

## 十二、最终 PR 收敛与交付报告

功能完成后重新扫描全部开放 PR。目标为仅 Scion PR #4 开放。保留该 PR 的独立范围，不合并、不删除、不改写。仍未解决的容量 Issue #13 等继续跟踪。

- 符合验收的已实现功能正式合并 main。旧 PR 若被后续分支吸收，应核对等价提交，说明实际合入位置后关闭。
- 不采用的实验须保留原始提交和证据，记录拒绝原因后关闭。证据 PR 须确保重要证据已归档或能稳定追溯后才可关闭，尤其审查 P5.5 额外证据 #18。[证据 #18](https://github.com/SKYdayDUCK/NovelFactory/pull/18)
- P6 真实本地授权未满足时，在正式报告或 Issue 写明 `BLOCKED_LOCAL_AUTHORIZATION`；禁止伪造结果，不应因此永久保留已达标的工程 Draft PR。禁止为剩一个 PR 而直接关闭未交付功能。

最终生成 `reports/integration/FINAL_DELIVERY_REPORT.md`，包含：

1. 完整阶段清单。
2. 各阶段 PR。
3. 实际合并 SHA。
4. 最终 main SHA。
5. 各阶段工程验收证据。
6. P5.7 新架构与功能。
7. 静态与动态 Agent 双模式结果。
8. 审核体系实际功能覆盖。
9. Agent/Skills 自动优化测试结果。
10. 日志、Token、费用和性能指标。
11. 已知风险及遗留性能问题。
12. P6 本地真实创作验收状态。
13. 最终开放 PR 和 Issue 清单。
14. 完整部署、使用及回滚说明。

## 十三、任务持久化、续办与完成条件

持续更新以下五份台账，以便跨会话接管：

- `reports/integration/CURRENT_STAGE.md`
- `reports/integration/DELIVERY_LEDGER.md`
- `reports/integration/PR_MERGE_MATRIX.md`
- `reports/integration/KNOWN_FAILURES.md`
- `reports/integration/NEXT_AGENT_HANDOFF.md`

每个稳定开发单元及时提交并 push；每个阶段完成时记录正式合并结果和新 main 的验收回执。完成单个 PR 后，只要授权、工具和执行环境允许，应继续下一阶段。遇实际平台权限、必需审批、真实本地环境或模型消费授权阻断，报告具体问题和可复现证据，不绕过限制，不宣称未完成事项已完成。

原始顺序为 W2 → NF Bridge → W3 → W4 → W5 → W6 → P5.6 主线验收 → P6 #17 → P6 #16 → P6 证据归档 → P5.7-S0～S5 → 最终主线回归 → PR 清理。接管以最新已签收阶段和真实仓库状态定位，按同一顺序续办。

最终同时满足 P5.6 全量进入 main、P6 现有可验证工程成果正式进入 main、P5.7 细粒度审核与动态 Agent/Skills 完整实现、动态开关默认关闭且可验证、必要日志和指标接入、逐阶段逐 PR 正式验收、开放 PR 只剩 Scion #4，以及 P6 真实本地创作和其他未完成项如实披露。以代码、数据库、日志、测试、PR 与最新 main 提交为准。
