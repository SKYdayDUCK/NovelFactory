# 重点论文卡片与实现边界

> 访问日期：2026-11-10。本文件只补齐工程采用信息，完整基线书目与既有实验摘要见 [v1.1 资料库](../baseline/v1_1/07_论文与技术资料库.md)。所有“本项目改编”均未宣称论文原系统已经实现。

## P-M｜DOME

**书目：** Qianyue Wang 等，*Generating Long-form Story Using Dynamic Hierarchical Outlining with Memory-Enhancement*，NAACL 2025，1352–1391。[会议页](https://aclanthology.org/2025.naacl-long.63/)｜[原文](https://aclanthology.org/2025.naacl-long.63.pdf)｜[作者仓库入口](https://github.com/Qianyue-Wang/DOME)。

**本轮核查：** 原文§4.1—4.2和摘要。先建粗纲，交替生成近端细纲与正文；MEM使用实体四元组，第四项是章节号，抽取与语义筛选仍调用LLM。论文报告连贯性等指标改善；本轮没有重新统计表格或运行仓库，不能给本项目同等提升承诺。

**本项目改编：** `planning`读取已接受快照和摘要依赖，生成带版本的近端计划；本地执行实体检索与过滤边界，复杂抽取交外部模型。角色视图不能包含计划结局；抽取永远是待对账材料。

**复现条件/风险：** 固定论文所用生成模型、提示词、相关性筛选及评审方法；若换中文/黑盒模型，标为迁移实验。检查同预算下重规划收益及废稿代价，另测章节位置与故事时间不同的案例。

## P-A｜Multi-Agent Based Character Simulation for Story Writing

**书目：** Tian Yu、Ken Shi、Zixin Zhao、Gerald Penn，In2Writing 2025，87–108。[会议页](https://aclanthology.org/2025.in2writing-1.9/)｜[原文](https://aclanthology.org/2025.in2writing-1.9.pdf)。

**本轮核查：** §3.2与限制段。角色先按发生顺序互动，随后按呈现顺序重写；导演选择发言者并提供动作命令。原研究假设场景时间不重叠，未显式强制隐私。摘要报告相对两种基线的偏好改善，不能据此推断本项目并发和权限可靠性。

**本项目改编：** 把模拟记录与文本产物分为两类版本；写作读取事件，不执行事件；导演提意图，世界裁决独立。相同事件可以在回忆和正文中多次出现。

**复现条件/风险：** 保留原计划、发生与呈现两种顺序、模型与评价提示；扩展到重叠场景属于新算法实验。验收重点是叙述重排后世界状态不变、披露顺序重新校验、角色秘密不泄漏。

## P-B｜BOOKWORLD

**书目：** Yiting Ran 等，ACL 2025，15898–15912。[会议页](https://aclanthology.org/2025.acl-long.773/)｜[PDF](https://aclanthology.org/2025.acl-long.773.pdf)｜[项目页](https://bookworld2025.github.io/)｜[作者仓库](https://github.com/alienet1109/BookWorld)。网页标题用 *for Story Creation*，PDF用 *for Creative Story Generation*。

**本轮核查：** PDF§3.1—3.3。角色/世界Agent协作，采用离散地图、同地点互动、以scene计旅行时间，再把模拟记录改写成故事。摘要报告在其比较设置中胜率75.36%；该数值是论文结果，本轮未复现，也不当作本项目预期胜率。

**本项目改编：** 地理、移动、消息和选择性角色激活进入世界服务；时间单位改为显式故事时钟；状态更新通过受控接受。作者仓库仅保留入口及基线核查结论，本轮不声明审查源码。

**复现条件/风险：** 保留源书抽取数据、地图、角色配置、脚本/自由模式及评价条件；自动抽取须人工/规则审查。优先验证跨场景资源守恒、旅行和临时NPC记忆交接。

## P-D｜DiriGent

**书目：** Chen Yang、Markus Gross、Rafael Wampfler，*Steering Narrative Agents through a Dynamic Cognitive Framework for Guided Emergent Storytelling*，AIIDE 2025。[作者发表页](https://cgl.ethz.ch/publications/papers/paperChe25b.php)｜[原文](https://cgl.ethz.ch/Downloads/Publications/Papers/2025/Che25b/Che25b.pdf)｜[代码入口](https://gitlab.inf.ethz.ch/prj-cgl/cgl-ai-character/dirigent)。

**本轮核查：** Narrative Generation、Story Enforcement与实验设置相关段。用信念/理想世界间张力解释行动；剧情强化可向角色候选注入指定动作，并循环增强张力至推进。实验采用五个提示、单主角框架；所报告改善不证明多主角长期自主性。

**本项目改编：** 只要求行动给出合法认知下的动机证据；作者承诺由卷规划与有界干预管理。允许拒绝和合理失败，重规划未执行未来，不强制角色选择目标动作。

**复现条件/风险：** 记录信念更新阈值、生成模型、干预次数、提示和评价者；代码入口本轮未核查可运行性或许可证。对照实验必须计入导演干预，防止把强制收敛误称角色自然决策。

## P-C｜ConStory-Bench / Lost in Stories

**书目：** Junjie Li 等，*Lost in Stories: Consistency Bugs in Long Story Generation by LLMs*，Findings of ACL 2026，8400–8428。[会议页](https://aclanthology.org/2026.findings-acl.410/)｜[PDF](https://aclanthology.org/2026.findings-acl.410.pdf)｜[作者项目页](https://picrew.github.io/constory-bench.github.io/)｜[仓库](https://github.com/Picrew/ConStory-Bench)。

**本轮核查：** §2与§6。基准包含2,000提示、四任务场景、五大类/十九子类；checker抽取、配对并给出文本证据。原文发现事实及时间错误突出，也说明英语/西方叙事和二元判断的限制，刻意延迟信息可能误报。本轮没有重跑模型排名。

**本项目改编：** 审核输出绑定版本和两侧证据，增加视角、意图性歧义、角色认知、世界推进和发布依赖维度。将确定性规则冲突与审美争议分开处理。

**复现条件/风险：** 固定任务、长度、评审模型、提示和分词统计；中文迁移需人工标注。不能把原来的单一一致性统计直接当小说发布门槛，更不能按模型建议直接修改世界事实。

## 补充资料怎样使用

以下为已阅读的v1.1记载，不代表本轮再次全文核查外站。来源、作者、版本和原始阅读范围见基线原章。

| 来源组 | 对应研发对象 | 保留的限制/实验要求 |
|---|---|---|
| P-R Re3、P-O DOC | 上下文编译、近端细纲与局部返工 | DOC控制器依赖原模型能力；黑盒API替代须单独验证 |
| P-G Generative Agents、P-Z Zep | 人物经历、反思、时态记忆 | 反思不是世界事实；新信息也不自动比旧信息真实 |
| P-I IPOCL、N3 Sabre | 关键场景动机/信念检查 | 符号域有限；允许失败的小说目标需额外机制 |
| N1 DODM、N2 Mimesis | 干预登记、意外后改线 | 不引入统一文学分数，不替换已接受的行动后果 |
| S01—S04 | 本地调度与因果测试 | FIFO不代表同刻争夺结果；开放世界完整性保证须有登记范围 |
| P-F MAST | 任务协议与失败分类 | 故障分布不直接迁移；用本项目夹具验证 |
| P-L、MemGPT | 上下文预算与记忆层次 | 实际模型位置敏感性需重测；MemGPT基线仅元数据/摘要 |
| L-T0—L-T6、L-C1—L-C2 | 文学档案、视角与整卷编辑 | 理论与作家经验不构成普适算法或强门禁 |
| L-E1—L-E3 | 质量/多样性分开评价、审核漏检与声音保护 | 英语短文或个人叙事结果不能替代中文长卷阅读实验 |

## 复现记录模板

每次实验独立保存：研究问题、采用来源及版本、代码commit、数据授权及划分、调用模型/配置、输入快照、提示/技能版本、预算、对照组、失败样本、指标定义、实际输出、评审条件、结论及不支持的外推。外部模型结果不保证逐字可重现；“保存原输出可审计”和“再次调用得到同输出”是两种不同能力。
