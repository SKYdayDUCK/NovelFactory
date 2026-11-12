# P5.9-L 创作指令审计

冻结基线 `434840593179e6c1faa6b7aeca3d7d747644506d`。从该树的 Git 跟踪清单核对 10 份 AGENTS.md（含根及 Workshop 作者入口）、1 份 SKILLS.md、7 份开发 Skill、10 份运行时角色、1 份公共角色入口、12 份运行时 Skill。下表的文件链接指向基线行，后续改动可直接对照。职责分配与交接见[责任矩阵](RESPONSIBILITY_MATRIX.md)。

静态评审逐项问三个问题：谁在什么输入下用它，输出由谁验收，容易在哪个场合照字面做错。再看方法是否能跨题材、例子是否真的需要、重审有无停止点、证据能否被该角色合法取得。现成步骤和可核查产物支持“可执行”；实际提升阅读体验需要另看全文盲评。

## 开发规范与作者入口

| 文件与定位 | 现有输入、责任与可验收结果 | 审计判断、边界或复核例 |
|---|---|---|
| [AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/AGENTS.md#L8) | 区分仓库开发与恢复作品，开发者读取目标目录祖先规范。 | 保留。开头的 P0–P4 说明是有日期背景的历史资料，项目现状取当前阶段台账。 |
| [SKILLS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/SKILLS.md#L21) | 根索引分别列 7 个开发和 12 个运行时技能，说明一次快照载入一个主技能。 | 历史 S0 曾报告漏项，当前基线已补齐。链接只负责导航。 |
| [benchmarks/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/benchmarks/AGENTS.md#L5) | 固定负载与分母，区分模型在途和领域接受。 | 保留。文学分数、实测 token、服务吞吐量分别报告。 |
| [contracts/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/contracts/AGENTS.md#L14) | 规定新契约的版本、权威与非法输入反例。 | 保留。Markdown 不增设必填字段，也无法注册岗位。 |
| [migrations/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/migrations/AGENTS.md#L3) | 隔离迁移验证、历史保留、可恢复进度。 | 保留。本文学迭代无需触发迁移。 |
| [src/novel_factory/domain/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/src/novel_factory/domain/AGENTS.md#L12) | 区分事实、认知、模拟和文学解释。 | 保留。人物自相矛盾的陈述可能是谎言，不能据此改历史。 |
| [src/novel_factory/application/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/src/novel_factory/application/AGENTS.md#L3) | 正式接受通过授权、版本复验和短事务。 | 保留。独立评委分数不能冻结作品。 |
| [src/novel_factory/infrastructure/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/src/novel_factory/infrastructure/AGENTS.md#L10) | 请求身份、不可变内容、费用与恢复。 | 保留。未知用量记未知，不能以字符数证明实际费用。 |
| [src/novel_factory/interfaces/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/src/novel_factory/interfaces/AGENTS.md#L3) | CLI、HTTP、MCP 共享应用语义，额外字段不能提权。 | 保留。对话中的角色职责不产生目录读取权限。 |
| [tests/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/tests/AGENTS.md#L11) | 文学评测留版本、证据和编辑决定；数据库另行验证。 | 保留。检索到一处原文不能替代阅读全书。 |
| [workshop/adapters/codex/AGENTS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/adapters/codex/AGENTS.md#L3) | 在 Workshop 作者项目一次提交恢复请求，重连复用 operation_id。 | 保留。NF 项目的恢复入口是 nf-resume；每次依据当前绑定选其一。 |

## 七个开发技能

| 文件与定位 | 现有输入、责任与可验收结果 | 审计判断、边界或复核例 |
|---|---|---|
| [nf-contract-change](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-contract-change/SKILL.md#L14) | 由契约差异与调用方推导合法例、非法例与兼容性。 | 在接口或版本发生变化时用；给写手增加一个自主选项不需调用。 |
| [nf-kernel-change](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-kernel-change/SKILL.md#L3) | 以数据边界及不变量为输入，产出有限修复和可重放反例。 | 保留工程术语；写手不能用该技能修改世界。 |
| [nf-concurrency-review](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-concurrency-review/SKILL.md#L15) | 同一读取切面检查时态、竞争和对账；计数分业务层。 | 适合调度、缓存与隔离变更；短篇盲评无需压测。 |
| [nf-pipeline-change](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-pipeline-change/SKILL.md#L1) | 以生产线变更和目标角色为输入，检查冻结、交接、权限及有限返工。 | 本轮最相关：核验目标稿、局部改动、复审闭合；优秀句子允许原样。 |
| [nf-recovery-check](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-recovery-check/SKILL.md#L17) | 从备份、对象及代际重建收据。 | 无故障无需演练；多人会话记录不能冒充数据库恢复记录。 |
| [nf-research-evidence](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-research-evidence/SKILL.md#L18) | 对来源、条件、复现状态和项目推论分别留据。 | 适合外部调查。研究报告使用 Markdown 登记，原 JSON 不在本轮修改范围。 |
| [nf-resume](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/.agents/skills/nf-resume/SKILL.md#L6) | 无参数恢复当前作品，报告实际醒来任务及状态，随后结束模型轮次。 | 按作者意图触发，不能充当小说编排或审稿技能。 |

## 公共协议与十个角色

| 文件与定位 | 现有输入、责任与可验收结果 | 审计判断、边界或复核例 |
|---|---|---|
| [README](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/README.md#L5) | 所有任务携带版本、证据和代际，权限由服务端签发。 | 卷导演目录行缺 volume_review；应对照 catalog。不要把任何相对链接描述为自动注入。 |
| [chief_editor](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/chief_editor.md#L23) | 具体争议、保护承诺及授权 → DecisionRecord。 | 既有规则允许裁量常规意见，未发现必须逐句审批的证据。重复意见归一次决议；有授权的编辑仍负责改稿安排。 |
| [world_designer](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/world_designer.md#L23) | 既有规则、资源距离和作用域 → 设定提案及未决前提。 | 保留。工艺、生活细节只有会改变持久事实才须上升为设定问题，避免人物成词条。 |
| [volume_director](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/volume_director.md#L24) | 卷契约、世界与人物目标 → 窗口、场景及重规划候选。 | 任务兼顾编剧规划是有意复用。补可改变路线的卷内兑现和可独立延展的支线；自由选择依旧归演员。上部模式遗漏见下。 |
| [character_actor](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/character_actor.md#L24) | 当时观察与信念、角色目标及资源 → 意图与可接受失败。 | 原文明确允许拒绝和离场。“演员必从大纲”无依据。可补同一句话面对不同人时说与不说各有何利害。 |
| [environment_director](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/environment_director.md#L7) | 背景过程、时窗、传播范围 → 环境或无变化的提案。 | 保留。照明故障能限制所有人的观察，不应为某人的英雄出场临时制造灾祸。 |
| [adjudication_advisor](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/adjudication_advisor.md#L25) | 汇集冲突组意图与已生效约束 → 有依据的联合后果候选。 | 保留。允许角色合理行动失败，角色只收经裁剪反馈。 |
| [writer](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/writer.md#L24) | 场景契约、获准稿件与已接受后果 → 正文、呈现关联、重大增量。 | 第24行给表达权，缺少可操作的选择实例。宜写清什么值得近写、何时概述、删解说时保住哪条线索。 |
| [evidence_maintainer](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/evidence_maintainer.md#L23) | 原文与事件版本 → 跨度、对账和记忆候选。 | 保留。人物说“我早知道”只证明发言，信念和读者披露需另有证据。 |
| [reviewer](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/reviewer.md#L25) | 冻结原文及授权面 → 带位置的分维度议题与无改动结论。 | 已区分文学建议与硬冲突，未发现仅看设定之强制规则。缺少自然度误报对照，可查跨声线复用及其效果。 |
| [literary_editor](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/literary_editor.md#L24) | 问题或有序全稿 → 方法、覆盖记录、结构编辑和卷结算。 | 原文已有优先级与接缝。宜把优先项绑定读者误解、最小改动与保留段，避免各维度轮流重写。 |

## 十二个运行时技能

| 文件与定位 | 现有输入、责任与可验收结果 | 审计判断、边界或复核例 |
|---|---|---|
| [prepare-world](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/prepare-world/SKILL.md#L20) | 需初始化或扩展 → 查现有规则、前提与世界变化建议。 | 查询无结果有覆盖条件。适配各题材，避免要求所有题材设计战力表。 |
| [plan-volume](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/plan-volume/SKILL.md#L25) | 需要卷纲或重规划 → 分开卷导演与总编模式，提交卷契约或取舍。 | 已有可行性和有限预演；补中段支线如何改变主线的选项及表达代价。 |
| [simulate-character](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/simulate-character/SKILL.md#L20) | 角色/组织要反应 → 从自己的信息提出意图、对白及可承受的损失。 | 可用同一人物在雇主与妹妹面前的不同沉默测试，避免声线凭口头禅区分。 |
| [adjudicate-actions](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/adjudicate-actions/SKILL.md#L25) | 资源冲突或语义未定 → 裁决或环境候选；角色模式决定权限。 | 已给等价细化边界；只需环境观察时不读整组私意。 |
| [write-scene](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/write-scene/SKILL.md#L20) | 从场景卡写稿或定向修订 → 正文、证据或事实增量。 | 动作、概述、对白、节奏仅概括点名；应允许压缩路程、放慢关系选择，不能把所有过程都场景化。 |
| [review-consistency](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/review-consistency/SKILL.md#L26) | 事实、认知、文学疑点 → 证据、类型、反解释和最小修改。 | 加正常用例。两人对同一往事说法不同，可是视角差；证据维护者仅交候选，无权正式审定。 |
| [maintain-memory](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/maintain-memory/SKILL.md#L25) | 更新摘要与跨卷关系 → 原文锚点、双方所知、不确定解释。 | 已记选择代价和未完后果，适合本轮复用；无需另建可写人物真相库。 |
| [research-literature](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/research-literature/SKILL.md#L8) | 有具体方法问题、样段与授权来源 → 可采用或弃用的 MethodNote。 | 原文列总编与 catalog 矛盾。可冻结该技能的是文学编辑；总编收方法材料后取舍。 |
| [edit-volume](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/edit-volume/SKILL.md#L18) | 整卷或跨卷问题 → ReadingCoverage、EditPlan、修订范围及结算。 | 已有先结构、后语言和停止点。加删改后读者能否理解同一因果的复核，留一段有效的反例。 |
| [volume-director-review](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/volume-director-review/SKILL.md#L6) | 已接受行动和实际裁决 → VolumeReviewOutput，重大偏离才提请求。 | 已有可执行反例：缺席的正式事件可触发重排，未接受的行动不行。普通表达意见留给写作。 |
| [review-change-point](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/review-change-point/SKILL.md#L10) | 重大偏离、精确计划与窗口引用 → APPROVED、RETURNED 或待作者决议。 | 给出抹去缺席代价的退回例。只在存在重大偏离时启动，普通润色无需上层审议。 |
| [roll-window](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/roll-window/SKILL.md#L10) | 已接受偏离及授权区间 → 提交有限窗口的新候选。 | 例中改派信使不预定信能送到；不受影响的线索不重写。 |

## 主线核对出的文字差异

1. [入口名单](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/README.md#L21) 与 [角色前部](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/volume_director.md#L9) 漏列 volume_review；同一角色末段和 [目录](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/catalog.json#L81) 均已包含。修复人读导航即可。
2. [研究技能头](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/skills/research-literature/SKILL.md#L8) 写总编可选方法，而 [总编真实名单](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/runtime/roles/catalog.json#L34) 没有该技能。将“采用方法”与“运行研究”明确交接可消除歧义。
3. 基线 10 份角色中仅总编、卷导演的头部版本与目录同为 2.1.1；另 8 份文件头为 2.0.0，目录为 2.1.0。任务应留目录版本及实际文本指纹；两次任务的同名同版本号无法证明提示字节相同。[加载位置](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/src/novel_factory/infrastructure/runtime_catalog.py#L54)。
4. 本地静态目录一次按任务选择一份角色与一份 Skill，打包资源优先；链接不会递归加载。已有任务沿用冻结字节；动态角色须按 Workshop/NF 既有登记与授权使用。只改 Markdown 的新评委指令能指导本轮独立会话，不能自行注册。Workshop 冻结清单绑定原 commit，不能用此次修改覆盖既有不可变记录。[Workshop 冻结协议](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/README.md#L17)。

## 生产架构、Workshop 与 Harness 的证据边界

| 文件与定位 | 现有输入、责任与可验收结果 | 审计判断、边界或复核例 |
|---|---|---|
| [docs/06_Agent编排与管理.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/docs/06_Agent编排与管理.md#L24) | 十族复用任务模式；任务、外部调用、领域结果分别跟踪。 | 角色复用允许，多人共用未过滤上下文不允许。 |
| [docs/07_剧情与文学生产系统.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/docs/07_剧情与文学生产系统.md#L29) | 从世界准备到跨卷维护的 14 项阶段契约。 | 三阶段是本轮验收分组。生成、接受、正文对账、编辑、冻结要逐项标实。 |
| [docs/P5.6_AgentWorkshop_需求分析.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/docs/P5.6_AgentWorkshop_需求分析.md#L5) | 组织岗位、作者接口、受限执行的设计依据。 | 设计需求不是运行回执。 |
| [docs/P5.6_AgentWorkshop_详细技术架构.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/docs/P5.6_AgentWorkshop_详细技术架构.md#L5) | 双服务边界及恢复设计。 | 编剧角色采用组织映射，不能据架构图推断某一席位已在执行。 |
| [docs/operations/workshop-delegation.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/docs/operations/workshop-delegation.md#L23) | 受限委托、确认与费用。 | 无绑定时可做外部多 Agent 文学实验；不可补写服务收据。 |
| [workshop/README.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/README.md#L43) | 单独授权可分享技能、独立评委收件。 | 确定性规则回执与独立模型阅读分开。 |
| [workshop/adapters/codex/dual-services.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/adapters/codex/dual-services.md#L1) | 组织员工和 NF 角色分别绑定。 | 不能将组织消息直接传给虚构演员。 |
| [workshop/docs/permissions.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/docs/permissions.md#L7) | 岗位身份、任期、生产者组、预算与审阅权。 | 终稿评委须未参与该稿生产。 |
| [workshop/docs/operations.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/docs/operations.md#L33) | 运维关联委托结果、保证重试和恢复。 | 运维权限不属于文学岗位。 |
| [workshop/benchmarks/README.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/workshop/benchmarks/README.md#L18) | 区分双服务场景与容量实验。 | 副本、并发数不能折算真实读者人数。 |
| [reports/harness/S0_ACCEPTANCE_MATRIX.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S0_ACCEPTANCE_MATRIX.md#L13) | 三臂口径、门槛、分母。 | 工程对照不会自动测文学偏好。 |
| [reports/harness/S0_ARCHITECTURE.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S0_ARCHITECTURE.md#L15) | 各阶段服务和权威数据分工。 | 用作路径证据。 |
| [reports/harness/S0_EVALUATION.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S0_EVALUATION.md#L43) | 冻结输入与独立评阅规范。 | 采用读者先独立通读。 |
| [reports/harness/S0_INSTRUCTION_AUDIT.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S0_INSTRUCTION_AUDIT.md#L5) | 旧提交逐文件核查，分观察、机制与假设。 | 旧导航问题已部分修复；本表对当前基线复核。 |
| [reports/harness/S1_CONTEXT.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S1_CONTEXT.md#L5) | 创作素材按 grant 与任务代际传递。 | 书库索引不会自己进到模型。 |
| [reports/harness/S1_RECOVERY.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S1_RECOVERY.md#L1) | S1 接管及恢复记录。 | 历史恢复不等于本轮就绪。 |
| [reports/harness/S2_PLANNING_SIMULATION.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S2_PLANNING_SIMULATION.md#L7) | 有限计划候选、隔离演算及正式采用流程。 | 推演结果不能充当真实剧情。 |
| [reports/harness/S3_LITERARY_REVIEW.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S3_LITERARY_REVIEW.md#L17) | 原文跨度、独立身份、复审、定向修订。 | 保留精确文本和编辑动作。 |
| [reports/harness/S4_DYNAMIC_RUNTIME.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S4_DYNAMIC_RUNTIME.md#L17) | 动态岗位版本登记和实际派发。 | 受控来源与运行时注册各自取证。 |
| [reports/harness/S5_ADAPTIVE_INSTRUCTIONS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S5_ADAPTIVE_INSTRUCTIONS.md#L1) | 从失败审核提候选，经独立评估及授权发布。 | 本轮实验失败后的根因和针对性改稿适用；全局自动提升不在授权范围。 |
| [reports/harness/S6_ACCEPTANCE.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S6_ACCEPTANCE.md#L17) | 逐项工程回归、数据库和双服务验证。 | 历史 CI 可作基线，不能计入本轮真实创作调用。 |
| [reports/harness/S6_ENGINEERING_COMPARISON.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S6_ENGINEERING_COMPARISON.md#L22) | 同场景三臂离线校准，试验单位和分母明确。 | 独立确认批数为零，不能代表本轮两部作品。 |
| [reports/harness/S6_METRICS.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S6_METRICS.md#L1) | 分开账本、调用、模拟与缺失分母。 | 费用未知不写零。 |
| [reports/harness/S6_OBSERVABILITY.md](https://github.com/SKYdayDUCK/NovelFactory/blob/434840593179e6c1faa6b7aeca3d7d747644506d/reports/harness/S6_OBSERVABILITY.md#L18) | 生产日志按授权关联任务、调用、证据。 | 文稿中不应出现后台报表语言，题材中角色合法行话例外。 |

开发技能中的审批、恢复和并发术语在各自用途上有明确作用。运行时的文学缺口主要在写手选择表达手段、卷级承接和审稿时落实原句作用。既有架构已给角色拒绝权、给编辑有限返工、给审核者无改动结论；这些能力保留，未发现足以另设常驻编剧、文风警察或新的逐章批准层的文字证据。
