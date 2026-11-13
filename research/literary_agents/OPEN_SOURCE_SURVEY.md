# 哪些外部写作方法值得放进 NovelFactory

核查日为 2026-11-13（北京时间）。检索中文网文技能、英文小说写作 Agent、长篇故事生成与整书评测，再回到作者仓库、论文与原始文件。七个仓库的链接固定到本次检索到的提交。下文的“采用”指本轮 Markdown 设计取舍。研究材料没有作为 Agent 指令执行，外部 Prompt、样本文字、代码、模型配置与整库语料没有移植。

## 方法与证据对照

| 项目与实读材料 | 项目怎样处理写作或评阅 | 可查的作品与效果证据 | 对 NovelFactory 的取舍 |
|---|---|---|---|
| lingfengQAQ/webnovel-writer，Claude Code 的 master/v6；GPL-3.0。读了上下文角色的输入优先级、写章任务书、审查与提交段落。[写章 Skill](https://github.com/lingfengQAQ/webnovel-writer/blob/e5b6a4b3798224dd2e78c806b6923abb3924dd56/webnovel-writer/skills/webnovel-write/SKILL.md) · [上下文角色](https://github.com/lingfengQAQ/webnovel-writer/blob/e5b6a4b3798224dd2e78c806b6923abb3924dd56/webnovel-writer/agents/context-agent.md) | 动笔前压缩本章目标、人物状态和紧迫线索；写后有单独审查和事实提取。文档明确区分跳过审查与审查通过，阻断问题定点处理。 | 流程、参数和回执可查。所读材料没有展示可与其他系统比较的完整中文长篇盲评。“200 万字”属于项目的能力定位。 | 采用任务书只带本章需要的材料、返工只碰受影响处。未来计划与已发生事实按本项目既有认知契约区分。无需引入另一套事实存储或每章复制该插件的全部关卡。 |
| Tomsawyerhu/Chinese-WebNovel-Skill，v2；仓库根未见通用许可证声明。读了路由与章末专项。[主 Skill](https://github.com/Tomsawyerhu/Chinese-WebNovel-Skill/blob/96709f477f4b59be856ebb10bec052dd0452b846/SKILL.md) · [章末工作规则](https://github.com/Tomsawyerhu/Chinese-WebNovel-Skill/blob/96709f477f4b59be856ebb10bec052dd0452b846/references/modules/chapter_ending/runtime.md) | 先辨认构思、卷纲、开头、单章或局部修改，再选择相应方法。章末诊断会区分本章尚未落定、缺少后续牵引，以及人为悬断过重。 | 有中长篇与短篇演示目录。未以所收录的第三方范文作为该系统的生成样本；演示和受控人评的证据等级不同。[仓库目录](https://github.com/Tomsawyerhu/Chinese-WebNovel-Skill/tree/96709f477f4b59be856ebb10bec052dd0452b846) | 采用“按实际症状选章末处理”。连载可以留下一个新处境；短篇可就此完结。保留方法描述，放弃素材检索、原文范本及统一强钩要求。 |
| danjdewhurst/story-skills；MIT。对照了有纲写作与探索式起草的触发条件、知识边界和写后处理。[章稿 Skill](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/chapter-writing/SKILL.md) · [探索式起草](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/discovery-drafting/SKILL.md) | 起草指定章时使用按故事位置裁剪的资料，避免查询全书文件泄露后事。伏笔安放和兑现各归其记录。探索式章稿写完再做倒推提纲、与既有事实对照。 | 提供能核查的文稿状态和操作步骤。所读技能未报告对照组、盲评或各工作模式胜率。 | 采用“章后问哪一项改变影响了后来”和“同一伏笔只登记一次”。角色自由选择限于其当时的知识。谜底依赖早期线索时先固定可观察证据；人物反应可留到写作时决定。不能把未经裁决的即兴想法覆盖进本项目正史。 |
| haowjy/creative-writing-skills；Apache-2.0。读了 character-sim 的真实方法和 critic 的职责。[角色试演](https://github.com/haowjy/creative-writing-skills/blob/0fb3c580a4e20435ddb0c0a744629b42a63c7307/skills/character-sim/SKILL.md) · [审稿角色](https://github.com/haowjy/creative-writing-skills/blob/0fb3c580a4e20435ddb0c0a744629b42a63c7307/agents/critic.md) | 角色试演使用当时所知、关系与压力，允许躲闪和误解；审稿任务按目的选择结构、声线或某一级编辑，定位原段后交回写手。 | 仓库可核对多角色与单会话两种运行方式；所读文件未提供小说整体质量的人评试验。 | 采用“同一件事在不同关系中怎样说”的试演。生产审稿人给问题位置、读者影响、修订范围；终稿另交新上下文。外部工程工具、模型策略和磁盘访问指令均不继承。 |
| facebookresearch/doc-storygen-v2；Apache-2.0。读了生成计划入口和逐节点展开程序，并核对 Re3 与 DOC 两篇论文。[计划入口](https://github.com/facebookresearch/doc-storygen-v2/blob/d092b42686bfc29ef2d610d5563e822ebf9d1451/scripts/plan/generate.py) · [节点展开](https://github.com/facebookresearch/doc-storygen-v2/blob/d092b42686bfc29ef2d610d5563e822ebf9d1451/storygen/plan/plan_writer.py) | 先产生地点与人物，再扩展事件节点并为节点附上场景和参与者。原论文的 DOC 另含生成过程中的详细控制；v2 是后续重写。 | 原论文有人评与公开示例。v2 仓库另有 7,000 对剧情方案的人评，资料单位为大纲对，不能当作 7,000 对完稿小说。[v2 人评说明](https://github.com/facebookresearch/doc-storygen-v2/blob/d092b42686bfc29ef2d610d5563e822ebf9d1451/README.md) | 只借用因果不清时加深节点、明确谁能在场的规划方法。舍弃逐 token 控制、向角色注入未来节点、生成多候选排序等实现。角色偏离原计划后由既有流程重排后续。 |
| Picrew/ConStory-Bench；代码 MIT。读了人物一致性评价 Prompt 的提取、排除与定位条件。[人物审查](https://github.com/Picrew/ConStory-Bench/blob/3f4195adda94d977ad9bd56f58ec51eaf1a1cd93/prompts/characterization.md) · [作者说明及结果入口](https://github.com/Picrew/ConStory-Bench/blob/3f4195adda94d977ad9bd56f58ec51eaf1a1cd93/README.md) | 按人物、事实、风格、时序及世界分项检索错误。人物检查要求原句与定位，允许空结果，强调回忆触发、讽刺和有意发展可以解释表面矛盾。 | 基准公布题目、模型故事和评判输出，目标长度为 8,000–10,000 个英语词；其指标关注文本一致性。[研究项目页](https://picrew.github.io/constory-bench.github.io/) | 采用成对取证并查其间的变化。伏笔没有明说、叙述者撒谎、角色改变态度都需先检验体裁解释。错误密度不能替代通读后的情节、语言与阅读感受。 |
| DingyiYang/LongStoryEval；所查仓库根未见通用许可证声明。读了首段、后续分段提示及调用脚本。[首段](https://github.com/DingyiYang/LongStoryEval/blob/ffc0c052664be345090a57b718fabc14b92c8136/evaluation_codes/prompt_template/aggregation/beginning.txt) · [后续分段](https://github.com/DingyiYang/LongStoryEval/blob/ffc0c052664be345090a57b718fabc14b92c8136/evaluation_codes/prompt_template/aggregation/incremental.txt) · [调用程序](https://github.com/DingyiYang/LongStoryEval/blob/ffc0c052664be345090a57b718fabc14b92c8136/evaluation_codes/eval_with_api/api_eval_with_chaps.py) | 对本段正文带入此前概述，分别判断结构、人物、语言、读者投入等维度。论文另比较整合分段意见、递进更新意见和摘要评价。 | 在 600 本书上使用读者评价对齐的研究，平均长度约 121k token。论文报告分段聚合对细节有优势、摘要法更节省；这是指定任务与样本上的结果。[ACL 2025](https://aclanthology.org/2025.acl-long.799/) | 采用逐段登记“读到了什么”，读完再串连跨章证据。冻结本项目的评分表和总评，禁止用摘要代读或简单平均章节分。未导入作品或评论数据。 |

## 论文结果能支持哪一步

Re3 对照同一基础模型的直接生成，作者报告判为全局情节连贯的比例高 14 个百分点，符合最初故事前提的比例高 20 个百分点。它同时改变计划、上下文重提、候选排序与事实编辑，无法把收益单独分给一个 Markdown 提示。DOC 在其 Re3 对照上报告情节连贯、贴合大纲和有趣三项分别提高 22.5、28.2、20.7 个百分点，作者项目页明确说明相关比较评的是 1,000–1,500 个英语词的段落。这些工作支持在**早先原因和后来结果接不上时**试用细化大纲，尚没有检验本项目两篇中文作品的文学质量。[Re3 原文](https://aclanthology.org/2022.emnlp-main.296/) · [DOC 原文](https://aclanthology.org/2023.acl-long.190/) · [作者展示与评估单位](https://storyrl.github.io/)

LitBench 以 2,480 对测试故事比较自动裁判与人类偏好。该设置下表现最好的原样通用模型裁判与人类一致约 73%，所训练的两种奖励模型约 78%。评委有分歧的空间。两部作品、同系列模型的独立上下文，足以做本轮门槛判定，但不足以估计中文小说读者的总体偏好或提示改进的平均因果效果。[LitBench 论文](https://aclanthology.org/2026.eacl-long.362/)

## 从一处具体问题选择方法

| 读到的故障 | 交给谁做何种最小改动 | 留什么证据供下一环节判断 |
|---|---|---|
| 中段两场都在打听同一条消息，读者两次看到同一个答案 | 卷导演让第二场出现新选择；若线索仍会改变一次决定，可保留查证的重复，压短经过 | 两场各自改变了谁能做什么。后一场删掉会丢掉哪项后果 |
| 人物忽然愿意冒险，仅因卷纲要求他在场 | 角色行动者用当时能知道的事试演一次拒绝和一次接受。导演比较两者造成的后续困难，再交现有裁决流程 | 本人看到的诱因；新增行为属于候选、已裁决或仅在草稿出现，三者需可辨 |
| 每章末都出现新的陌生来电 | 写手回看前后两章，用已发生的决定、物件的去向或关系变化承接，连载中的实质线索仍可保留 | 上一章的末段与下一章开头分别交给编辑，验读停顿是否成立 |
| 两条支线隔四章再出现，人物位置或知情范围冲突 | 一致性审查者记录旧段、新段和中间是否发生交接。文学编辑判断有意隐瞒、认知盲区能否解释读者体验 | 两处正文位置，具体相斥的命题，可能解释，哪一段需要复核 |
| 单章干净利落，整书仍给人原地踏步的感觉 | 全文审稿人串连开篇承诺、中段职责改变、结尾实际付出，再指出最先可调整的一章 | 首中末三处原文与读者期待怎样变化，和章评的均值分别处理 |

样本以能核对原文件的项目为主，包含四个写作工具和三个生成或评估实现。研究没有部署外部插件，没有测其吞吐或在相同题目下重跑各库。两个未查到仓库级许可的项目按公开研究资料处理，仅归纳方法。上述许可证记录只涉及注明的仓库或代码；演示正文、论文以及另存的数据集需分别核对授权。
