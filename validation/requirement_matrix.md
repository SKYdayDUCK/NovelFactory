# 用户需求、交付文件与验收追溯矩阵

> 本文保留原 v2.0 任务的覆盖映射。统一时间、动态关系和高并发补充要求的完整覆盖见 [R1_REQUIREMENTS](R1_REQUIREMENTS.md)；当前总量与验证状态以 [REPORT](REPORT.md) 为准。

> v2.0｜2026-11-10｜本文件记录设计覆盖位置，不授予实现验收通过状态。  
> 业务案例统一见 [case_matrix.json](case_matrix.json)：44 个 v1.1 案例与 16 个 v2 新增案例，全部为 `PLANNED_NOT_EXECUTED`。独立内存实验、Schema 校验和文档审查不改变该状态。

## 1. 使用方法与判定边界

从下面的需求行进入相应技术文档、机器契约、角色或技能，再在案例矩阵中按案例 ID 取得原始输入、预期、来源与阶段门槛。表中的“验收入口”是未来执行范围，不能仅凭这些案例编号存在就声称满足需求。

P0 冻结环境、契约与夹具；P1 验证最小可信内核和首次异步任务门禁；P2 形成真实 5–10 场景短卷并完成整卷编辑，才算 MVP；P3 扩大并行、细化依赖并验证等效宏微替换；P4 验证跨卷、连载和规模恢复。完整阶段定义与依赖见 [11 研发路线](../docs/11_研发路线与测试计划.md)。

原始基线、案例期望和文学分歧不得为制造通过而删改。未来执行记录应独立记录输入 WorldRef、规则/文本版本、实际输出、持久结果、反例、证据、未覆盖范围及结论；语义和文学判断允许待定及有理由的分歧。

## 2. 十个技术领域、论文体系与研发规划

| ID／用户要求 | 主要设计及机器契约 | 角色、技能与开发约束 | 验收入口及阶段 |
|---|---|---|---|
| R01 完整软件架构：模块、依赖、事务、同步/异步、恢复、单机部署与扩展 | [01 架构](../docs/01_详细技术架构.md) §2–7；[02 模块](../docs/02_代码架构与模块设计.md) §1–6；[目录骨架说明](../project_layout/README.md)。core 的 TaskSpec、TaskResult、CommitReceipt 与 ErrorEnvelope；领域核心不依赖模型 SDK | [根 AGENTS](../AGENTS.md)；[应用层规范](../src/novel_factory/application/AGENTS.md)、[基础设施规范](../src/novel_factory/infrastructure/AGENTS.md)；[恢复技能](../.agents/skills/nf-recovery-check/SKILL.md) | F01/F02/F09/F10/F11；V2-12/V2-15。P1 真实 PG、任务与恢复；P2 真实调用；P4 规模测量。 |
| R02 统一数据与知识：实体关系、规则、历史、过程、认知、资源、证据、计划、正文、分支、派生失效 | [03 数据模型](../docs/03_数据模型与知识图谱.md) 全文；[09 记忆](../docs/09_长期记忆与检索.md) §1–8；[core.schema](../contracts/core.schema.json) 的 WorldRef、OccurrenceTime、QueryAnswer、EvidenceRef、NormalizedEffect、ProcessRecord；[literary.schema](../contracts/literary.schema.json) 的 MemoryCandidate | [世界设计角色](../runtime/roles/world_designer.md)、[记忆维护角色](../runtime/roles/evidence_maintainer.md)；[prepare-world](../runtime/skills/prepare-world/SKILL.md)、[maintain-memory](../runtime/skills/maintain-memory/SKILL.md)；[领域规范](../src/novel_factory/domain/AGENTS.md) | T01/T02/T04/T06/T09/T11/T12；F05/F07/F13/F14；V2-01。P1 结构、时态与视图；P2 语义提取及文学证据。 |
| R03 版本与正式提交：不可变事件、重放、CAS、批准、合并、Retcon、过期/重复、世界接受与正文发表分离 | [04 版本体系](../docs/04_事件溯源与版本体系.md) 全文；core 的 WorldChangeSet、EventSelectionOperation、RecordSelectionProposal、AcceptWorldRequest、PrepareRetconRequest、PromotionRequest、ReleaseManifest；[报告契约](../contracts/report_templates.md) 的 RetconPlan／ImpactReport／RevalidationReport | [内核修改技能](../.agents/skills/nf-kernel-change/SKILL.md)、[契约修改技能](../.agents/skills/nf-contract-change/SKILL.md)；[迁移规范](../migrations/AGENTS.md)、[契约规范](../contracts/AGENTS.md)。运行时角色只有候选能力，正式接受由服务执行 | T05/T08；F03/F04/F07/F09/F10/F12/F17/F18；V2-02/V2-03/V2-04/V2-07/V2-14。P1 基础修订、幂等与原子性；P2 正文对账；P3 精细重验证。 |
| R04 世界模拟与调度：因果时间、跨区影响、共享资源、联合裁决、未求值、宏微、失败恢复 | [05 调度](../docs/05_世界模拟与调度引擎.md) 全文；core 的 OccurrenceTime、ProcessRecord、ProcessAdvance、TriggerEvaluation、ActionProposal、AdjudicationProposal、AdvancePrepareRequest；[04](../docs/04_事件溯源与版本体系.md) 原子接受 | [角色行动者](../runtime/roles/character_actor.md)、[环境导演](../runtime/roles/environment_director.md)、[裁决者](../runtime/roles/adjudication_advisor.md)；[simulate-character](../runtime/skills/simulate-character/SKILL.md)、[adjudicate-actions](../runtime/skills/adjudicate-actions/SKILL.md) | T07/T09/T12；S01–S08；V2-03/V2-05。P2 少域联合裁决与宏观后纯呈现；P3 才开放经验证的等效替换。 |
| R05 多 Agent 管理：独立角色/复用模式、生命周期、任务、权限、失败、审批与返工 | [06 编排](../docs/06_Agent编排与管理.md) 全文；core 的 TaskSpec、TaskResult、ContextBundle、Approval、ReviewIssue；[角色目录](../runtime/roles/catalog.json) 与[角色使用约定](../runtime/roles/README.md) | 十个角色族的 task_modes 与 requested_tools 均在目录登记；[总编角色](../runtime/roles/chief_editor.md)只在授权策略内作编辑决议；[SKILLS](../SKILLS.md)区分开发与运行时 | S08；F01/F02/F03/F08/F16/F19；V2-09/V2-11/V2-15。P1 首次异步即有 fencing；P2 真实 Host/模型身份路径；P3 压力与收益对照。 |
| R06 剧情与文学生产：世界准备、卷契约、角色互动、重规划、并行写作、对账、多尺度审核、整卷及跨卷 | [07 生产系统](../docs/07_剧情与文学生产系统.md) 全文；[09](../docs/09_长期记忆与检索.md) 文学证据；core 的 SceneContract、ReviewIssue、ReleaseManifest；literary 的 VolumeContract、WindowPlan、ReadingCoverage；报告契约的 AlignmentReport | [卷导演](../runtime/roles/volume_director.md)、[写作者](../runtime/roles/writer.md)、[审核者](../runtime/roles/reviewer.md)、[整卷编辑](../runtime/roles/literary_editor.md)；[plan-volume](../runtime/skills/plan-volume/SKILL.md)、[write-scene](../runtime/skills/write-scene/SKILL.md)、[edit-volume](../runtime/skills/edit-volume/SKILL.md) | T03/T08/T10；S04/S06/S07；L01–L05；F15/F16/F17；V2-07/V2-08/V2-10/V2-16。P2 必须交真实短卷与前后编辑稿；P4 再扩大跨卷。 |
| R07 MCP、SDK、REST、内部函数：接入取舍、工具契约、权限、错误及部署 | [08 接口](../docs/08_MCP与工具接口.md) 全文；[tool_catalog](../contracts/tool_catalog.json) 是工具请求/响应入口；[core](../contracts/core.schema.json) 请求 DTO；[官方平台核查](../research/official_platform_notes.md) | [接口层规范](../src/novel_factory/interfaces/AGENTS.md)；[契约修改技能](../.agents/skills/nf-contract-change/SKILL.md)；角色 requested_tools 与服务端 grant 取交集，技能不加权 | F01/F02/F06/F08/F11/F19；V2-02/V2-11/V2-14。P1 统一服务契约；P2 实际 Host/profile 互操作。外部投递一旦启用必须先过 F11/F18。 |
| R08 AGENTS.md：根与局部开发规范、自主范围、数据约束、测试提交、真实高风险审批 | [根 AGENTS](../AGENTS.md)；[领域](../src/novel_factory/domain/AGENTS.md)、[应用](../src/novel_factory/application/AGENTS.md)、[基础设施](../src/novel_factory/infrastructure/AGENTS.md)、[接口](../src/novel_factory/interfaces/AGENTS.md)、[迁移](../migrations/AGENTS.md)、[契约](../contracts/AGENTS.md)、[测试](../tests/AGENTS.md)；[02](../docs/02_代码架构与模块设计.md)开发完成定义 | [运行时角色契约](../runtime/roles/README.md)独立管理，不继承开发 shell/迁移权限；普通修复、实验、开发分支提交可自主完成，超出现有授权的真实高风险操作才请求许可 | P0-02 的目标 Host 实际加载与规范审查；F19/V2-11 检查越权路径。AGENTS 自主性不是数据库单元测试能证明，需保留实际任务演练记录。 |
| R09 SKILLS.md 与模块技能：索引、触发、输入输出、工具边界、渐进加载和平台适配 | [SKILLS](../SKILLS.md)；[运行时技能目录](../runtime/skills/catalog.json)；[Codex 官方核查](../research/official_platform_notes.md)；[Claude 适配说明](../adapters/claude/README.md) | 开发主源 `.agents/skills/` 的五项技能；运行时九项技能主源 `runtime/skills/`。共享技能的受限角色模式在 catalog 和 SKILL 明确；由 [nf-pipeline-change](../.agents/skills/nf-pipeline-change/SKILL.md)规范流程改动 | P0-02 验证目标 Host 的发现、同名去重、按需加载与失败；F19/V2-11 检查技能不能扩权。当前文件存在不等于平台兼容已通过。 |
| R10 文学研究与长期质量：方法库/作品风格隔离、声线、弧线与情绪、多尺度审核、分歧、模板化与整卷上下文 | [07](../docs/07_剧情与文学生产系统.md) §8–12；[09](../docs/09_长期记忆与检索.md) §2、6–10；[10](../docs/10_论文与技术资料库.md) §5；literary 的 MemoryCandidate、ReadingCoverage；core 的 ReviewIssue | [literary_editor](../runtime/roles/literary_editor.md)、[reviewer](../runtime/roles/reviewer.md)；[research-literature](../runtime/skills/research-literature/SKILL.md)、[review-consistency](../runtime/skills/review-consistency/SKILL.md)、[maintain-memory](../runtime/skills/maintain-memory/SKILL.md)、[edit-volume](../runtime/skills/edit-volume/SKILL.md) | L01–L05；F15/F16；V2-08/V2-10/V2-13/V2-16。P2 原文证据与完整阅读；P3 同预算配对比较；P4 跨卷。没有文学总分公式。 |
| R11 论文与技术证据：保留基线、分类、方法/结果/限制、复用机制、模块映射、复现条件及官方来源 | [10 论文库](../docs/10_论文与技术资料库.md)；[paper_cards](../research/paper_cards.md)；[sources](../research/sources.json)；[基线论文库](../baseline/v1_1/07_论文与技术资料库.md)；[ADR](../docs/12_架构决策与风险.md) | [nf-research-evidence](../.agents/skills/nf-research-evidence/SKILL.md) 与 [research-literature](../runtime/skills/research-literature/SKILL.md)。DOME、BOOKWORLD、DiriGent、Multi-Agent Character Simulation、ConStory-Bench 的采用边界与实验结论按资料库标注 | L04/V2-13 检查方法升级流程；P3 的预演、记忆、审核与并行对照检验项目收益。来源核查不等于论文复现；未复现数据不得升格为本项目测量。 |
| R12 研发结构、分阶段任务、测试与早期端到端原型 | [02](../docs/02_代码架构与模块设计.md)模块矩阵；[11](../docs/11_研发路线与测试计划.md)任务卡、依赖、60例、样卷脚本、性能与出口；[目录骨架](../project_layout/README.md)；[12](../docs/12_架构决策与风险.md)未决机制；[案例矩阵](case_matrix.json) | [tests/AGENTS](../tests/AGENTS.md)、[nf-kernel-change](../.agents/skills/nf-kernel-change/SKILL.md)、[nf-pipeline-change](../.agents/skills/nf-pipeline-change/SKILL.md)、[nf-recovery-check](../.agents/skills/nf-recovery-check/SKILL.md) | T01–T12、S01–S08、L01–L05、F01–F19、V2-01–V2-16；另执行基线 §6 与11 §6整体样卷。P1 工程内核通过也不能代替 P2 MVP。 |

## 3. 最终十三项系统交叉审查

下表给出已经采用的设计约束及未来反例入口。设计审查修正不能替代运行证据；全部业务案例仍未执行。

| 审查项 | 当前设计防线与明确出处 | 反例／验收入口 |
|---|---|---|
| C01 世界真相是否存在多个权威来源 | [03](../docs/03_数据模型与知识图谱.md) §1、3：不可变记录与版本选择；图、摘要、状态是可重建投影。record_operations 与事件效果的认知身份归一。[04](../docs/04_事件溯源与版本体系.md)唯一接受事务 | T01/T08/T11；F05/F15；V2-01/V2-07。摘要、正文和重复认知输入不得另改真相。 |
| C02 世界时间与数据库版本是否混用 | [03](../docs/03_数据模型与知识图谱.md) §2：WorldRef／StoryPoint／OccurrenceTime分型；[05](../docs/05_世界模拟与调度引擎.md) §3–4：排他 frontier、约束求解与工作流时间分离 | T02/T12；S02/S03/S08；F07。增加入站下界恰等 frontier 的端点反例，不用完成速度生成因果phase。 |
| C03 计划、模拟与正式事件是否混淆 | [03](../docs/03_数据模型与知识图谱.md) §5：六平面与正交状态；[04](../docs/04_事件溯源与版本体系.md) §2–3：分支身份及选择；literary 的 VolumeContract／WindowPlan没有世界写权 | T03；S01/S06；L05；V2-01/V2-10。SELECTED计划、候选载荷与正式事件分别核验。 |
| C04 角色是否能读取越权信息 | [06](../docs/06_Agent编排与管理.md) §6；[09](../docs/09_长期记忆与检索.md) §3、5、8；[character_actor](../runtime/roles/character_actor.md)；服务端绑定角色/分支/时点，工具、引用、错误、缓存和Host额外能力共同检查 | T02/T04/T11；S03；F06/F08/F13/F19；V2-09/V2-11。猜中秘密与服务泄露分开判定。 |
| C05 调度与行动裁决是否重叠 | [05](../docs/05_世界模拟与调度引擎.md) §1、4–5：调度决定需要处理的边界与冲突组，裁决提出联合后果，提交服务接受；[06](../docs/06_Agent编排与管理.md)区分任务与世界状态 | T07；S01/S02/S08。到期、求值成功、动作成功与正式接受不是同一状态。 |
| C06 宏微模拟是否重复扣除资源 | [04](../docs/04_事件溯源与版本体系.md) §5；[05](../docs/05_世界模拟与调度引擎.md) §6：发生身份、每组唯一表示、每构建效果账；P2只纯呈现，P3才验证替换 | T08/T09；S04；V2-03/V2-05。终态相同而中间消息/认知不同必须拒绝伪等效。 |
| C07 正文修改是否错误撤销世界事件 | [04](../docs/04_事件溯源与版本体系.md) §9；[07](../docs/07_剧情与文学生产系统.md) §6、9–10；SceneContract、AlignmentReport与ReleaseManifest固定对账依据 | T08；L02；V2-07/V2-08/V2-16。删告知描写不能令角色恢复未知；新增持续效果回世界提案。 |
| C08 并行写作是否使用不一致快照 | [07](../docs/07_剧情与文学生产系统.md) §5–7；[04](../docs/04_事件溯源与版本体系.md) §3、9：批次pin、章节交接、最终release统一WorldRef及重验证；core ContextBundle/SceneContract | T02/T07；F03/F04/F07/F17；V2-06/V2-08。旧批次批准不能直接拼成新release；实际同线接缝须在P2完整阅读。 |
| C09 Agent是否可绕过正式提交 | [06](../docs/06_Agent编排与管理.md) §6；[08](../docs/08_MCP与工具接口.md) §4、7；[角色catalog](../runtime/roles/catalog.json)；AcceptWorldRequest与Approval绑定内容、任务、版本和授权 | F01/F03/F08/F19；V2-02/V2-11。角色自称、技能文字和候选字段都不授予写库或canon切换能力。 |
| C10 MCP、Skills和业务服务是否重复 | [02](../docs/02_代码架构与模块设计.md) §2–5；[08](../docs/08_MCP与工具接口.md) §1、5；[SKILLS](../SKILLS.md)：适配器只转发，技能只指导操作，业务服务统一规则 | P0-02契约生成与依赖审查；F19/V2-11；P2 Host/profile互操作。需比较SDK/REST/MCP行为，不能仅测某一种调用路径。 |
| C11 AGENTS是否过度限制开发自主性 | [根AGENTS](../AGENTS.md) §2：普通修复、隔离实验、开发分支提交可自主；修改核心文件本身不触发许可；已有授权不重复询问；局部规范引用相应边界 | P0真实开发任务演练：普通修复应能完成，真正超授权的生产覆盖/扩权应停在具体可审阅方案。F19/V2-11用于越权反例，不能单独证明自主性。 |
| C12 工程规则是否僵化创作 | [03](../docs/03_数据模型与知识图谱.md) §1.1；[07](../docs/07_剧情与文学生产系统.md) §5、8–9；[09](../docs/09_长期记忆与检索.md) §6–7；ReviewIssue区分硬冲突、语义疑点与文学选择 | L01–L05；S07；V2-09/V2-13/V2-16。合作也可表现成长，丧友后发笑不自动报错，普通细节无需逐名词入图。 |
| C13 关键架构假设是否缺验证依据 | [12](../docs/12_架构决策与风险.md) §4列风险、最小实验及降级；[11](../docs/11_研发路线与测试计划.md) §3、7–8分工程/语义/文学证据；[实验说明](../experiments/README.md)限定内存机制结论 | F04/F15/F16；S05；V2-05/V2-11/V2-12。真实PG、Host隔离、开放语义依赖、中文文学收益和长篇恢复均保留后续验证门槛。 |

## 4. 交付物定位与使用完整性

| 用户要求的实际交付 | 当前文件位置与使用方式 |
|---|---|
| 十二篇技术文档 | `docs/01`–`docs/12`，按本表进入；[02](../docs/02_代码架构与模块设计.md)给模块职责、输入输出、依赖、错误和测试边界 |
| 根与局部开发规范 | [AGENTS.md](../AGENTS.md)及第2节R08列出的七份局部规范；它们是简洁约束，技术细节链接到docs |
| 技能索引与独立技能 | [SKILLS.md](../SKILLS.md)、五项开发技能与九项运行时技能；平台主源和适配方式在索引中固定 |
| 运行时角色契约 | [runtime/roles/catalog.json](../runtime/roles/catalog.json)及其中十份role Markdown；运行时能力仍由服务端grant决定 |
| 关键Schema、接口与正反例 | [contracts/README](../contracts/README.md)、[core](../contracts/core.schema.json)、[literary](../contracts/literary.schema.json)、[tool_catalog](../contracts/tool_catalog.json)、[报告模板](../contracts/report_templates.md)、[core示例索引](../contracts/examples/index.json)、[literary示例索引](../contracts/examples/literary_index.json)与[拒绝例索引](../contracts/invalid_examples/core_index.json) |
| 架构、依赖、ER和流程图 | Mermaid内嵌于[01](../docs/01_详细技术架构.md)运行结构、[02](../docs/02_代码架构与模块设计.md)依赖、[03](../docs/03_数据模型与知识图谱.md)ER、[04](../docs/04_事件溯源与版本体系.md)提案状态、[05](../docs/05_世界模拟与调度引擎.md)联合交互、[06](../docs/06_Agent编排与管理.md)任务状态、[07](../docs/07_剧情与文学生产系统.md)产线、[11](../docs/11_研发路线与测试计划.md)阶段依赖 |
| 项目代码目录骨架说明 | [project_layout/README](../project_layout/README.md)与[02](../docs/02_代码架构与模块设计.md)；当前目录规范不表示业务实现已存在 |
| 基线保留与论文资料 | [baseline/v1_1/README](../baseline/v1_1/README.md)、[原测试文件](../baseline/v1_1/09_压力测试与端到端案例.md)、[sources](../research/sources.json)、[paper_cards](../research/paper_cards.md)；逐文件读取记录见[baseline_reading_audit.json](baseline_reading_audit.json) |
| 可复现的小实验与业务验收区分 | [experiments/README](../experiments/README.md)和[test_mechanisms.py](../experiments/test_mechanisms.py)只验证局部机制；[case_matrix.json](case_matrix.json)保留60项业务验收的未执行状态 |

## 5. 本矩阵尚不能证明的事项

文件和契约已可以指导分工；真正的数据库并发、权限隔离、故障恢复和MCP/Host互操作仍须按P1/P2执行。剧情模拟收益、语义抽取漏检、文学记忆的锚定风险、多Agent审核效果与长篇文学质量，需要真实样卷、独立证据和对照，不能由本矩阵或文件数量推断。

若后续改变某项核心机制，更新对应需求行、Schema/角色/技能和案例映射，并记录新增证据；保持原始案例来源与历史执行记录可追溯。不得把未执行改成通过来满足阶段出口。
