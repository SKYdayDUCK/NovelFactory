# S0 指令与执行路径逐文件审计

冻结主线 `276820316129d546b0a13118bcbe283b06507795`。审计共 77 份源文件。下表给出该提交的具体行号与 Git blob 前 12 位，每项完整 40 位对象名、可见性和后续核验同存于机器清单。根 `SKILLS.md` 的 S0 文字改动可在本轮差异复核；本报告记录的是改动前原文。[逐项源清单](S0_INSTRUCTION_SOURCES.json) · [冻结主线](https://github.com/SKYdayDUCK/NovelFactory/tree/276820316129d546b0a13118bcbe283b06507795)

## 哪些文字到达模型

| 证据层次 | 本次能确认的行为 | 判读限制 |
|---|---|---|
| 项目开发与作者入口 | 开发 Host 按任务读取项目规范与技能。作者“继续”走 NF 项目绑定；Workshop 作者项目也有自己的恢复入口。 | 这是这些文件的适用范围。没有每次开发会话自动加载的完整记录。 |
| 源码确认的运行时选取 | 安装包中存在 `data/runtime` 时优先从包读；源码部署从 `runtime/` 读。服务检查角色存在、模式属于该角色、选定技能在允许列表，默认取第一技能。一次快照载入一份完整角色正文和一份完整技能正文，记录目录版本、受限模式、工具上限与指纹。 | 某个组合可以构造快照，不代表生产派发过。技能列表是角色范围，具体模式的合适技能由调用者显式选择。Markdown 链接到公共 README 或带“参考”的文档不触发递归读入。 |
| 最终请求与切面 | 网关将角色与技能全文、受限模式、任务身份和选定输出 Schema 放入 system。user 带 assignment、context、read_cut、获授权的原始材料；已登记工作流可增补编辑/联合裁决材料。派发前再核任务、代际、租约、权限、冻结模型配置和快照指纹。 | 模型普通文本请求的可执行工具为空。特定演员路线另有单工具只读 Host，控制器固定读取与模型受限选择各有独立执行证据。 |
| 已观察的历史执行 | 一次演员固定读取确有 `nf_context_get`、0 个工具模型轮和 1 次合成供应商请求；另一个选择案例有 `nf_query_batch`、2 次合成请求与 2 条已结算记录。宴会历史 8 次请求大小为 12,101–19,640 UTF-8 字节，含不同大小的 Schema。 | 原记录证明这些路线运行过。字节不是 Token，合成回执不能推导外部账单、全量模式的使用率或文学效果。 |
| 文体风险与假说 | 写手角色及技能的文字都同时包含创作方法、领域状态、工具协议和退出条件。固定正文中可以定位平台式叙述和有情境作用的工艺术语。 | 文字共现只支持提出对照实验。固定样卷的原作者预先写入六场正文，样卷无法识别是哪条提示导致某句话。 |

## 核对出的导航与版本差异

1. 冻结的根技能索引列六项开发技能，实际目录七项；遗漏项是作者恢复用的 `nf-resume`。运行表列九项，漏掉 `volume-director-review`、`review-change-point`、`roll-window`。第 22 行仍写“按任务加载一个主技能及必要辅助技能”。本轮根导航已补这四个入口并按实际单次快照纠正文字。[原根索引](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/SKILLS.md#L5-L36) · [实际加载器](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/infrastructure/runtime_catalog.py#L32-L65)
2. 公共角色 README 的卷导演清单漏列 `volume_review`。角色正文末段及实际 catalog 有记录。技能 `research-literature` 第 8 行写到总编可选方法，但总编目录只允许 `plan-volume`、`edit-volume`、`review-change-point`。按此冻结版本，直接给总编选择研究技能会收到 `ROLE_MODE_DENIED`；研究角色可以递交材料供总编取舍。[人读索引](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/README.md#L19-L28) · [卷导演补充](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/volume_director.md#L46-L50) · [总编允许项](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/catalog.json#L29-L39) · [研究技能原文](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/research-literature/SKILL.md#L6-L12)
3. 十份角色中，总编与卷导演的文首版本和目录同为 2.1.1；其余八份文首写 2.0.0、目录写 2.1.0。运行时的 `role_contract_version` 取目录值，`role_contract` 仍含原始标题。核对身份须同时查内容与目录，不能以标题推断加载版本。Workshop 清单固定在 `df1e0cc904e3b94a360d86e3a3e00bfec3258673`；本次独立计算两目录、十角色和十二技能的原字节 SHA-256，24/24 与其记录相等。[快照字段](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/infrastructure/runtime_catalog.py#L44-L65) · [Workshop 固定来源](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/contracts/nf-catalog.json#L2-L98)

## 开发与作者项目

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [AGENTS.md L3–82](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/AGENTS.md#L3-L82) | `ede2e8d8afc8` | 开发 Agent 读局部规范、区分作者恢复和开发；世界、正文、内部发布、外部交付分开。第 4 行是历史接管说明，第 82 行才指向当前台账。 | 保留服务不变量；阶段现状由 CURRENT_STAGE 读取，勿复制到人物说明。 |
| [SKILLS.md L3–55](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/SKILLS.md#L3-L55) | `9b6bdb5c6b44` | 开发表仅列六项，作者恢复另在根入口。运行表仅列九项，实际目录十二项；第 22 行称可加载必要辅助技能，snapshot 实际只冻结一项。 | S0 校正导航，运行时仍以固定提交代码为准；任意后续组合加载须另行验证。 |
| [.agents/skills/nf-concurrency-review/SKILL.md L14–27](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-concurrency-review/SKILL.md#L14-L27) | `fcbc1ec729fa` | 固定读取切面，检查空查询、ABA 与并发；容量与模型在途量分别取证。 | 以后改编译或缓存时运行其并发反例；演员无须看到锁竞争术语。 |
| [.agents/skills/nf-contract-change/SKILL.md L14–30](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-contract-change/SKILL.md#L14-L30) | `2c9eee532cc0` | 明确契约归属、版本、可信身份与非法输入；区分 Schema 符合和授权。 | 转换给角色的用户可读材料时保留原 DTO、授权和错误码对照。 |
| [.agents/skills/nf-kernel-change/SKILL.md L16–32](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-kernel-change/SKILL.md#L16-L32) | `54e6adcda5c0` | 区分世界、正文与派生结果；用真实数据库验证幂等与跨分支反例。 | 将不变条件保在接受服务，给编剧角色的材料展示已批准后果。 |
| [.agents/skills/nf-pipeline-change/SKILL.md L14–31](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-pipeline-change/SKILL.md#L14-L31) | `a66e552031c0` | 覆盖交接、固定快照、披露、有限返工和创作建议，明定工程与文学独立验收。 | 后续改稿同时回归无权限变化、分段证据与独立审读。 |
| [.agents/skills/nf-recovery-check/SKILL.md L14–30](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-recovery-check/SKILL.md#L14-L30) | `060b1224af62` | 恢复重放不调用模型；旧代排除，UNKNOWN 对账后处理。 | 每次指令发布须能按原输入重放，失败与 UNKNOWN 计入比较。 |
| [.agents/skills/nf-research-evidence/SKILL.md L14–28](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-research-evidence/SKILL.md#L14-L28) | `cf338a6143f6` | 核对原始来源，区分研究报告、项目推论与实测，不可自动提升为业务规则。 | 作品风格建议附适用情境和反例，保留作者取舍。 |
| [.agents/skills/nf-resume/SKILL.md L3–6](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/.agents/skills/nf-resume/SKILL.md#L3-L6) | `e14515ec7093` | 作者从项目 MCP 单次唤醒，报告实际状态；管理待办去重，不认领人物、不找管理员令牌。 | 保留作者入口与人物连续对白的身份隔离，并把该项补入导航。 |

## 每个运行时角色

下列文件属于可被选中的模型材料。角色名字、说明中的工具、Markdown 中的链接都不授予服务权限。

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [runtime/roles/adjudication_advisor.md L3–46](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/adjudication_advisor.md#L3-L46) | `de72b4730453` | 联合裁决使用同组意图与成本，产出候选，定序不能取决于先交稿者。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 以竞争同一物件、双方均合理为例，裁决说明与演员所见分开。 |
| [runtime/roles/character_actor.md L3–46](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/character_actor.md#L3-L46) | `89f0910970c8` | 角色仅凭自身 BELIEF，允许拒绝、等待和离场；材料夹杂全知时报告污染。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 按当时可见消息核对角色动机；保留不知道、误信与故意隐瞒三种差别。 |
| [runtime/roles/chief_editor.md L23–52](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/chief_editor.md#L23-L52) | `b269adc13856` | 可就创作分歧作决定，重大偏离绑定版本；总编无世界写权，返工有上限。 | 盲审检查批准/退回的具体代价及真分歧，留存普通稿原样通过。 |
| [runtime/roles/environment_director.md L3–48](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/environment_director.md#L3-L48) | `bf2970ad056e` | 允许过程变化或无变化；对环境的授权不包括读取密封意图和决定胜负。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 用潮闸木楔受力与环境无变化成对检查，防止每场都用灾祸逼选。 |
| [runtime/roles/evidence_maintainer.md L3–48](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/evidence_maintainer.md#L3-L48) | `cf85549346d9` | 抽取、对账和摘要绑定精确原文；证据候选不能替人审稿。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 保存原句、人物所知时点、反证和不确定范围；正文不写断言分类。 |
| [runtime/roles/literary_editor.md L3–48](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/literary_editor.md#L3-L48) | `827c770fec84` | 分研究、结构、接缝与跨卷；可保留原稿，全文覆盖不足不能宣布整卷通过。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 后续抽查是否为个别声音举出好例，保留有意停顿、直接抒情与多视角差异。 |
| [runtime/roles/reviewer.md L3–46](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/reviewer.md#L3-L46) | `e381e4077a41` | 先看证据；区分合理误信、倒叙、谎言、硬问题和审美分歧。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 用误报与漏报相配的盲审，记录原句、权限、反解释与建议影响。 |
| [runtime/roles/volume_director.md L13–50](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/volume_director.md#L13-L50) | `653e2c3d39e3` | 先看局部影响与可行路径；正文末尾另列 volume_review、replan 的不同技能与任务，前部名单未完整反映它们。 | 导览与 catalog 对齐；看改走书信是否保留缺席代价及未受影响节点。 |
| [runtime/roles/world_designer.md L3–46](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/world_designer.md#L3-L46) | `8e943fda3ffd` | 设定、历史与未知分开；新规则提交候选，不能靠补设定抹去代价。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 提供各类型世界的制度和物理依据；允许世界中的技术职业说专业话。 |
| [runtime/roles/writer.md L3–46](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/writer.md#L3-L46) | `82ac1d955405` | 强调自主对白、意象、留白和普通细节，同时要求提交版本、工具、事件关联及重要增量。 第3行标2.0.0，目录供快照使用的版本为2.1.0。 | 对照同一场景的正文、提交外壳与变更说明，检查系统词进入叙述的具体位置。 |

## 每个运行时技能

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [runtime/skills/adjudicate-actions/SKILL.md L17–27](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/adjudicate-actions/SKILL.md#L17-L27) | `56a19bfd88d0` | 明确拆分环境过程任务与独立联合裁决；环境模式无法取得整组人物意图。 | 候选返给人物前按视角裁剪；保留竞争失败与合法未知。 |
| [runtime/skills/edit-volume/SKILL.md L18–39](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/edit-volume/SKILL.md#L18-L39) | `1d5b721d8629` | 按结构、接缝、语言顺序通读并比较小范围方案；不按章平均分判整卷。 | 保留无需修订组和不同声音，衡量改动是否确实有益。 |
| [runtime/skills/maintain-memory/SKILL.md L17–27](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/maintain-memory/SKILL.md#L17-L27) | `a2d19c5a220d` | 维护者可核事实，文学编辑只能用已交付材料；解释、读者反应假设与事实分列。 | 用同一段对白分别存事实、解释与反例，不把推断升为人物心理事实。 |
| [runtime/skills/plan-volume/SKILL.md L17–43](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/plan-volume/SKILL.md#L17-L43) | `ca263a43c204` | 总编的共享模式仅取舍，卷导演提出结构与窗口；失败路线不得靠倒改历史修好。 | 用一条偏离线和一条无关线测定向重规划，控制信息留在规划输出。 |
| [runtime/skills/prepare-world/SKILL.md L18–38](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/prepare-world/SKILL.md#L18-L38) | `9ecaee5334e6` | 供给、距离、交通、通信和制度要支撑情境；查不到不自动等于不存在。 | 让不同类型沿用不同术语；以水利身份和行动目的区分行话与平台词。 |
| [runtime/skills/research-literature/SKILL.md L8–38](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/research-literature/SKILL.md#L8-L38) | `8abe4f2d113e` | 从原始出处提取有条件的写作方法，明确反例；不全书禁用某种表达。头部提及总编方法选择，当前总编 catalog 未列该技能。 | 按实际可冻结角色修正说明；研究建议只对授权作品范围生效。 |
| [runtime/skills/review-change-point/SKILL.md L6–12](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/review-change-point/SKILL.md#L6-L12) | `01eb1aa7bc24` | 在总编 editorial_decision 绑定候选与参考线，只判该偏离，采纳由受权服务执行。 | 保留批准、理由充分的退回和必要的作者决定；不强行回归原计划。 |
| [runtime/skills/review-consistency/SKILL.md L17–43](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/review-consistency/SKILL.md#L17-L43) | `b1e20dc7941f` | 区分硬冲突、语义疑点、文学选择和变更请求；引文、事实锚点与反解释均有位置。 | 把有意误信和精确数错配成负/正例，禁止凭词频直接扣分或拒收。 |
| [runtime/skills/roll-window/SKILL.md L6–12](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/roll-window/SKILL.md#L6-L12) | `01d17d72adbc` | 只改下一有限窗口，保留原参考线和真实后果；尚未送达不能提前写成回信。 | 成对比较可回归与合理分化，检查成本是否仍在。 |
| [runtime/skills/simulate-character/SKILL.md L18–38](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/simulate-character/SKILL.md#L18-L38) | `23d291058a5a` | 亲见、传闻、猜测和无知分开；人物可拒绝导演倾向，不能自判行动成功。 | 测试有权限与未送达消息两种条件，相同角色可有不同选择理由。 |
| [runtime/skills/volume-director-review/SKILL.md L6–12](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/volume-director-review/SKILL.md#L6-L12) | `a868c3436b8d` | 先等行动与正式裁决屏障，以受权证据评估重大偏离；不足可保留未知。 | 单列未接受提案、真实意外、普通表达，防止把每一变化升级。 |
| [runtime/skills/write-scene/SKILL.md L12–49](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/write-scene/SKILL.md#L12-L49) | `9ee37f717a31` | 既要求声音、相邻原文、节奏与留白，也重复身份核对、工具和绑定说明；无词汇禁表。 | 用准确引文比较控制语外溢与人物正当行话，候选提升由盲读证实。 |

## 目录及公共说明

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [runtime/roles/README.md L3–32](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/README.md#L3-L32) | `7959d5aaa918` | 公共协议、角色目录和查询边界；目录中的卷导演模式略去 volume_review。被 Markdown 链接，加载器没有展开该文件。 这是供人及宿主参考的说明页，所选角色引用它也不会自动读入。 | 修订人读索引时以 catalog 复核模式，必要约束同时由服务验证。 |
| [runtime/roles/catalog.json L3–325](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/roles/catalog.json#L3-L325) | `783e8de88fd3` | 列十个角色、各自 task_modes、skill_names 和工具请求上限；默认技能取列表第一项。 | 保持顺序与显式选择分别可追溯；验证每个角色/模式的实际派发入口。 |
| [runtime/skills/catalog.json L5–162](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/skills/catalog.json#L5-L162) | `e2ce7574c29b` | 十二个技能和四种受限角色用法；目录元数据供选择和收窄工具说明。 | 对照角色目录、手册、共享模式真实来源，避免把全部技能正文塞入一次模型任务。 |
| [runtime/query_profiles.json L3–239](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/runtime/query_profiles.json#L3-L239) | `3cc6cd14eb4c` | 十类角色查询平面、操作和 recipe 上限；ACTOR 仅 BELIEF。 | 测试角色、时间、scope 交集；资料是否有趣不能改变可见范围。 |

## Workshop 的岗位映射与审核人

| Workshop 岗位 | NF 映射 | 实际责任 |
|---|---|---|
| `workshop_manager`、`author`、`review_lead`、`maintenance` | 空 | 管理、作者决定、分派独审和运维各自沿组织授权处理。作者恢复使用单独的项目绑定。 |
| `screenwriter` | `volume_director`、`writer` | 通过预先登记的单一槽位派发某个角色；两个名字不会变成一个同时掌握规划、写作、验收的角色。 |
| `chief_editor`、`volume_director`、`character_actor`、`world_designer`、`environment_director`、`adjudication_advisor`、`writer`、`evidence_maintainer`、`reviewer`、`literary_editor` | 各同名映射 | 仍逐次校验组织、安装、员工任期、可指派关系、作品/分支、代际、预算与 NF 身份。 |

四个预设的默认席位数分别是 Minimal 8、Standard 11、Long-form 13、Research-Audit 14，包含相同的三个演员初始席位。环境导演与独立裁决角色虽可映射，四个默认预设均未给他们单列员工。这些数是默认组织编制，不能充当实际运行角色数或真实读者数。[默认席位](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/src/agent_workshop/organization.py#L83-L113)

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [workshop/contracts/nf-catalog.json L2–98](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/contracts/nf-catalog.json#L2-L98) | `f3df6cdf33ca` | 清单标源 df1e0cc。对冻结主线实算两目录、十份角色与十二份技能的 SHA-256，24/24 一致。 | 后续若更新技能，保持旧清单可复验并重新登记选择的角色版本。 |
| [workshop/scripts/freeze_nf_catalog.py L9–22](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/scripts/freeze_nf_catalog.py#L9-L22) | `be55650a5e63` | 从指定 checkout 的 HEAD 以 git show 读取完整原字节并写 SHA-256；不加载模型。 | 记录 NF 版本与目录指纹，勿用“同名”代替字节一致。 |
| [workshop/tests/test_catalog.py L7–16](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/tests/test_catalog.py#L7-L16) | `957430560ea5` | 协商测试比对两个目录指纹、NF 提交及 10/12 计数，未逐个运行角色任务。 | 同时保留内容级比对和实际执行证据。 |
| [workshop/src/agent_workshop/organization.py L14–113](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/src/agent_workshop/organization.py#L14-L113) | `3871e2865603` | 十五种组织岗位映射十种 NF 角色，编剧映射卷导演与写手；管理者、作者、审查主管、运维映射空。四个 preset 建立不同员工席位。 | 每次委托记录实际员工、独立身份组与 NF 角色；一人双映射不能自审。 |
| [workshop/src/agent_workshop/delegation.py L43–93](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/src/agent_workshop/delegation.py#L43-L93) | `647bd6e59f03` | 通过安装、员工任期、NF_ROLES、指派路由及金额/Token 检查后才能占用预置槽。 | 用错任期、错角色和超额槽验证拒绝，不靠文字要求分权。 |
| [workshop/src/agent_workshop/presets.py L110–138](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/src/agent_workshop/presets.py#L110-L138) | `52ce3edbeb54` | 预览摘要、组织版本、管理岗位及冲突共同约束切换；预设是组织结构。 | 记录各批次预设或明确沿用同一预设，避免团队不同导致假提升。 |
| [workshop/src/agent_workshop/reviews_skills.py L236–428](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/src/agent_workshop/reviews_skills.py#L236-L428) | `2f15e066be67` | 确定性审核计算库存、请求完成/重复效果、Token 核账；审稿排除原生产者及身份组，分开 RULE、HUMAN 和后续委托。 | 盲读另存实际人评；工程规则 PASS、主管任命皆不能代为填写。 |
| [workshop/docs/permissions.md L3–12](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/docs/permissions.md#L3-L12) | `df4adfd7b8ab` | 将组织名册和消息与 NF 的人物信念分开，披露受任期、作品/分支与任务代际约束。 | 保留跨服务负例；独立读者只收正文及个人空表。 |
| [workshop/README.md L15–23](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/README.md#L15-L23) | `aa95c4c2daae` | 协商版本与指纹用于验证来源，工具与数据访问仍由各服务签发的活会话决定。 | 比对指纹后还需核对给定任期与任务，禁止把清单当通行证。 |
| [workshop/adapters/codex/AGENTS.md L1–3](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/adapters/codex/AGENTS.md#L1-L3) | `9a6289befb51` | 作者项目另有单次 aw_resume_project 入口；不得把组织私信转给虚构人物。 | 区分作者恢复、组织安排与演员台词，重试保留原 operation_id。 |
| [workshop/adapters/codex/dual-services.md L3–20](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/workshop/adapters/codex/dual-services.md#L3-L20) | `cdb1a53225d7` | 双服务 Codex 的员工和角色绑定独立；真实双服务只读回执能见两种工具边界。 | 比较实验复用同样隔离配置并登记，变权另走正式审批。 |

## 从编译、Host 到审核与冻结

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [src/novel_factory/infrastructure/runtime_catalog.py L17–66](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/infrastructure/runtime_catalog.py#L17-L66) | `af0ab3d9b7b6` | 优先选安装包资源，选单角色与单技能；显式技能须在角色允许列表。没有 mode→skill 唯一映射，调用方负责选择；正文内链接不展开。 | 以后改编译必须比原始两个文本、版本、最终 snapshot 摘要和实际派发。 |
| [src/novel_factory/application/runtime_tasks.py L47–104](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/runtime_tasks.py#L47-L104) | `fad5b4386f66` | 仅控制者可建任务；演员自身 BELIEF、角色和故事时点绑定，能力按切面签发，再钉住预算与 runtime_contract_version。 | 使用现有任务和预算，不向生成者授予正式接受或任意检索。 |
| [src/novel_factory/application/queries.py L580–697](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/queries.py#L580-L697) | `8c066ab33ce3` | 编译时校验授权、代际和固定时点；构建区分 BELIEF/FACT/PLAN 的片段、依赖、许可来源。 | 层次拆分保留固定 WorldRef、知识边界、来源和未知状态。 |
| [src/novel_factory/application/query_policy.py L14–42](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/query_policy.py#L14-L42) | `ecfee1c9c730` | 从打包或源码文件读 profile。查询逐次核 grant、角色上限、操作、recipe，PLAN 和图读另有限权。 | 误配材料应由服务拒绝；文本无权豁免。 |
| [src/novel_factory/application/agent_gateway.py L81–234](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/agent_gateway.py#L81-L234) | `db11c16088d1` | 冻结 AGENT_RUNTIME/AGENT_INPUT，派发重核租约、cut、内容及模型配置指纹。system 拼入完整角色+技能、受限模式、身份及 Schema；user 限任务、context、read_cut、授权材料及专用材料。 | 比较发给适配器的分层原件及哈希，保留未返回与超限样本；不得从说明文字推断真实模型已依从。 |
| [src/novel_factory/application/prompt_schema.py L32–66](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/prompt_schema.py#L32-L66) | `b60af8e62bda` | 仅保留选定契约可达定义并选较短表达；结构化输出身份仍在信封。 | 报告 Schema 占用与角色/技能占用两项，不用缩短字节冒充文学改善。 |
| [src/novel_factory/application/scene_runtime.py L202–341](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/scene_runtime.py#L202-L341) | `db3a02539f38` | 登记时将技能显式传给任务；联合裁决、卷导演复核及各自屏障分别建立。 | 同一批冻结登记参数，不用先返回的角色结果决定联合胜负。 |
| [src/novel_factory/application/editorial_successors.py L43–78](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/editorial_successors.py#L43-L78) | `7cd0b3e47a6c` | 后继为 replan 明选 roll-window，总编的 editorial_decision 明选 review-change-point。 | 验证所选技能与角色/模式组合；文档中链接的技能不自动并入。 |
| [src/novel_factory/application/editorial_handoff.py L84–115](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/editorial_handoff.py#L84-L115) | `df86bc0cbe90` | 从本次已登记的合法 route/材料构造编辑专属输入；偏离、计划和参考线按目标处理。 | 将编辑可见的诊断与作者读者包分开，防止诊断渗入叙述。 |
| [src/novel_factory/infrastructure/codex_host.py L148–219](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/infrastructure/codex_host.py#L148-L219) | `6753441ea134` | 临时 HOME 与 CWD，项目指导字节上限 0，关 shell/apps 等，临时只读线程只收到请求摘要；桥向后端交付已冻结请求。 | 工程证据分别记录宿主线程、后端实际输入和供应商收据。 |
| [src/novel_factory/infrastructure/role_container.py L7–24](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/infrastructure/role_container.py#L7-L24) | `f608f44dedc5` | 角色读取由 bubblewrap 限制宿主挂载与命名空间，运行目录与 IPC 单独暴露。 | 验证角色不能从工作树补读开发技能或其他人员资料。 |
| [src/novel_factory/interfaces/role_context_host.py L78–145](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/interfaces/role_context_host.py#L78-L145) | `867de3321d6f` | 角色只读 Host 每次只启用一个工具；核验库存后调用，限定 nf_context_get 或 nf_query_batch。 | 把授权查询的输入输出与模型普通文本请求分开记账。 |
| [src/novel_factory/application/actor_lookup.py L34–154](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/actor_lookup.py#L34-L154) | `3f950bde5e01` | 可选演员查询先由登记的独立选择轮返回受限问法，服务执行一次查询后加入合法结果。 | 区分控制器固定读取、模型选择读取和通用工具能力；统计跳过、拒绝和两阶段成本。 |
| [src/novel_factory/application/narrative.py L581–759](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/narrative.py#L581-L759) | `68ce062d78ba` | 从精确正文与世界绑定形成对账；正式接受需要相应对账证据。 | 库存/认知硬冲突与语气意见分开，保存候选与拒绝原因。 |
| [src/novel_factory/application/editorial.py L591–661](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/editorial.py#L591-L661) | `0bfb5cc0804d` | 整卷批准复核正文接受、审稿矩阵、结构→接缝→语言的精确稿件链、硬问题解决与职责分离；同一服务另限轮次/问题数。 | 保存每轮变化、未改稿和未决项；人类文学取舍另登记。 |
| [src/novel_factory/domain/editorial.py L78–113](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/domain/editorial.py#L78-L113) | `04adbe65819e` | 相同问题按证据指纹去重，结构先于语言，逐场/章/卷/相邻接缝覆盖。 | 负例包含摘要冒充通读、重复问题、新版正文沿用旧批准。 |
| [src/novel_factory/application/release.py L442–467](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/application/release.py#L442-L467) | `ccb7f7857fa1` | 按作品授权读冻结版本，导出按有序引用构造正文，权限核查独立于角色话术。 | 盲评只发冻结 UTF-8 正文；每份记录摘要，不携带审计信息。 |
| [scripts/p6_export_release.py L20–54](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/scripts/p6_export_release.py#L20-L54) | `4cb037e64be7` | 公开作者入口验证绑定分支，使用 ReleaseService 导出原文与 SHA-256。 | 两位读者只拿相同字节与各自空表，审计日志留协调端。 |

## 可追溯的运行与文学证据

| 源文件与行号 | Git blob 前 12 位 | 文件既有行为或缺口 | 后续核验或调整 |
|---|---|---|---|
| [evidence/p5/verification/supervised_role_read.json L3–16](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p5/verification/supervised_role_read.json#L3-L16) | `a2915a729232` | 一次固定 nf_context_get、工具轮 0、一次合成供应商请求、一条账；付费模型 NOT_RUN。 | 用于校准“工具读取已观察”的口径，不能外推所有人所有任务。 |
| [evidence/p5/verification/actor_tool_choice.json L2–25](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p5/verification/actor_tool_choice.json#L2-L25) | `eef5a5970fbc` | 合成演员选择查询与最终候选共两次供应商请求，两条成功记录；无外部账。 | 把选择与生成分帐，外部费用没有账单时填 null。 |
| [evidence/p5/final/prompt-sizes.json L253–301](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p5/final/prompt-sizes.json#L253-L301) | `efe790bcac98` | 历史宴会8个合成请求分别记 UTF-8 大小与 Schema 大小，最小 12101、最大 19640 字节。 | 新批次逐项记字节及可核对 Token；历史字节不能换算供应商实际 Token。 |
| [evidence/p2/batch3/short-volume/run/release.txt L7–53](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p2/batch3/short-volume/run/release.txt#L7-L53) | `7331370c61bc` | “联合裁决只批准”等工程口吻与木楔顶闸、汛务凭证的情境用语出现在同一原文。 | 按人物、场景、引文和后果给出可反驳判断；保留反例。 |
| [evidence/p2/batch3/short-volume/run/author_feedback.md L3–13](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p2/batch3/short-volume/run/author_feedback.md#L3-L13) | `afc34253ab2f` | 作者阅读全文、阅读日期、具体问题及最终结论都为空。 | 读者自行填写，待返还不计为0分。 |
| [evidence/p2/batch3/short-volume/run/run_manifest.json L2–15](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p2/batch3/short-volume/run/run_manifest.json#L2-L15) | `c880b1478c67` | 六场夹具、文学 NOT_EVALUATED、作者 PENDING_AUTHOR；同文件预算段记录十五次夹具和零真实调用。 | 冻结输入和正文原件分开，报告工程与文学两项状态。 |
| [src/novel_factory/orchestration/short_volume_fixture.py L1–25](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/orchestration/short_volume_fixture.py#L1-L25) | `8e2cc0cbcad8` | 正文是固定编写的 TEST_FIXTURE，有角色目标、六场标题和固定全文；没有现场创作因果识别能力。 | 仅作样例和工程负控，不能作为新旧生成质量对比。 |
| [src/novel_factory/orchestration/short_volume.py L116–130](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/orchestration/short_volume.py#L116-L130) | `f7d5d9b3e9a3` | 首轮“重复未增加信息”来自固定审稿夹具，保留有意重复的反解释。 | 引用来源而不冒认人工审读，零新证据的返工记为负担。 |
| [reports/handoff/P6_RUNTIME.md L3–11](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/reports/handoff/P6_RUNTIME.md#L3-L11) | `f02b8eba7764` | 生产模型请求为0，未取得生产正文/账单/独立文学评分；既有研究计划给出3–5角色、10–20场、两名读者。 | 日后若开展，需要独立的真实预算与人员登记；继承历史计划要注明。 |
| [reports/handoff/P6_READER_FORM.md L3–16](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/reports/handoff/P6_READER_FORM.md#L3-L16) | `fbfdaba1ce42` | 两份独立空表格式按五维记录1–5、对应场次及理由，分差至少2分保留原意见复核。 | 按正文摘要绑定独立填写，不公开另一位意见或工程判断。 |
| [docs/P6_本地真实模型创作交接.md L87–117](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/docs/P6_本地真实模型创作交接.md#L87-L117) | `6e61e58d904a` | 作品/分支受权作者导出，私有日志和绑定单独保存，两人拿同一文本及各自空表。 | 日后执行用独立化名、读者回收及凭据隔离；S0只核对设计。 |
| [contracts/examples/literary_source.txt L1–2](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/contracts/examples/literary_source.txt#L1-L2) | `9e685459d6b5` | 白桥尚未通，粮在南岸；沈砚提出有条件合作，只有这两行构成原文证据。 | 与摘要对读，避免把同意合作解释成已宽恕。 |
| [contracts/examples/literary_summary.txt L1–1](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/contracts/examples/literary_summary.txt#L1) | `0ea37eea16d0` | 摘录明确限制“原谅守军”的推论，不能充当原文全文送达记录。 | 审阅允许“证据不足”结论，评分须有冻结原句。 |

### 怎样判断工程措辞

《潮闸余灯》第 19–23 行用木楔受力、铜件顶闸和汛务旧规说明林澈为何必须牺牲铜印；第 25 行用“联合裁决只批准”解释粮数，第 39 行用“正式成员关系”交代贺宁入社，第 43 行直接说明更换视角重述不会再次交粮。同一场景能同时含合理职业语言与后台说明。核对时先确定说话者、受众、当时所知和该句所起作用，再对照同一世界事实在人行动、对白与叙述中的不同呈现。此处未给作品打分。[逐句正文](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/evidence/p2/batch3/short-volume/run/release.txt#L17-L45)

另一项合成宴会中，事务登记出自模拟供应商返回，书信改期出自编辑提案；这些文字所在字段没有冻结小说正文的身份。白桥合同原文里沈砚只同意有条件合作，配套摘要保留他是否原谅守军为未知。历史审核原件出现过对首场重复句的意见，源码明示这是固定意见。能重建交接，仍须等待真实独立阅读才能判断文风。[宴会输入与输出](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/tests/integration/test_p5_autonomous_banquet.py#L43-L103) · [白桥原文](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/contracts/examples/literary_source.txt#L1-L2) · [固定审稿意见](https://github.com/SKYdayDUCK/NovelFactory/blob/276820316129d546b0a13118bcbe283b06507795/src/novel_factory/orchestration/short_volume.py#L121-L130) · [后续评价协议](S0_EVALUATION.md)
