# P6 职业与题材知识研究记录

核对日期为 2026-11-13。运行时目录保留原始来源的哈希、作者、许可、定位与适用范围。下列材料用于提炼方法，未将教材、小说样例或第三方 Prompt 复制到模型上下文。工程验证仅覆盖装载、作用域和冻结；对成书效果的评估留待双批次实验。

## 从开源实现取得的方法

在 GitHub 搜索 fiction writing skills、story planning skills、academic writing skills、agent reflection、literature review skills、character development fiction、writing skills、multi-agent story writing。核读八个指定仓库，另取反思实现、独立编辑入口和多 Agent 正文生成器。表中链接固定到当时的提交，文件 Git blob 标识见运行时索引。

|仓库及实读文件|本项目采纳或限定的方法|
|---|---|
|[anthropics/skills · skills/skill-creator/SKILL.md](https://github.com/anthropics/skills/blob/2c5958ecac7cee2b583967a2c4981d0f4a75c579/skills/skill-creator/SKILL.md)|把发现、运行和评测拆开；记录有、无改动时任务输入一致的对照结果。|
|[openai/plugins · plugins/superpowers/skills/writing-skills/SKILL.md](https://github.com/openai/plugins/blob/d420d13051bffe4543ed39e30fa46b87148e47dd/plugins/superpowers/skills/writing-skills/SKILL.md)|先取能暴露失误的情形，再写规则并检查负例。仅仅改写说明不足以证明 Skill 改进。|
|[danjdewhurst/story-skills · docs/getting-started.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/docs/getting-started.md)|首次运行和日常维护有不同交接点；写一章后核对已落盘的状态。|
|[KalarisLabs/research-agent-skills · skills/citation-verification/SKILL.md](https://github.com/KalarisLabs/research-agent-skills/blob/802bbea44e551abac24d6856f83fc55149bbdba3/skills/citation-verification/SKILL.md)|书目标识能解析与证据支持主张是两次检查。本项目还需原文位置、同一版本与权限。|
|[LeonChaoX/qinyan-academic-skills · skills/沁言学术skills/qinyan-nature-review/SKILL.md](https://github.com/LeonChaoX/qinyan-academic-skills/blob/df5a498a81e0f9c8f79d814446dcf9e9b8f68888/skills/%E6%B2%81%E8%A8%80%E5%AD%A6%E6%9C%AFskills/qinyan-nature-review/SKILL.md)|同一问题保持编号，分别记录不同审核视角、分歧、证据位置及关闭条件。|
|[IorioD/claude-academic-research-skills · academic-pipeline/references/ai_research_failure_modes.md](https://github.com/IorioD/claude-academic-research-skills/blob/5a1a5a687602c79c31b1ab8603ebcf8de65a9579/academic-pipeline/references/ai_research_failure_modes.md)|阶段完整性审查回到原始记录；可疑结果要保留为待复核，禁止从异常表现直接提升方法。|
|[FonaTech/Academic_Writing_Skills · skills/sci-manuscript-review/SKILL.md](https://github.com/FonaTech/Academic_Writing_Skills/blob/086d3a5cc8d1a002a1f0dcb8df4d193b8423eb1f/skills/sci-manuscript-review/SKILL.md)|先核验对象及版本，再核验支持范围；作者和审阅者对同一记录分别留下结果。|
|[dvdsosa/scientific-writing-skills · skills/sciwrite-prose/SKILL.md](https://github.com/dvdsosa/scientific-writing-skills/blob/2eb42c9afb4c9edd4ad5c3a4ae9351e139bae0a0/skills/sciwrite-prose/SKILL.md)|优先处理结构，再看局部语言；脚本提示的风格异常需要人或独立编辑在语境中判断。|
|[danjdewhurst/story-skills · skills/series-continuity/SKILL.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/series-continuity/SKILL.md)|跨卷延续需要有出处的事实，未解决事项、公开信息和愿望要分别记录。|
|[danjdewhurst/story-skills · skills/genre-craft/SKILL.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/genre-craft/SKILL.md)|题材方法沿 Skill 分流，按当前需求读取具体参考而非拼接全部题材。|
|[noahshinn/reflexion · alfworld_runs/generate_reflections.py](https://github.com/noahshinn/reflexion/blob/218cf0ef1df84b05ce379dd4a8e47f17766733a0/alfworld_runs/generate_reflections.py)|失败轨迹与此前策略可以作为下一次提问的输入；模型生成的反思仍须验证。|
|[Nanako0129/sepia · skills/sepia-review/SKILL.md](https://github.com/Nanako0129/sepia/blob/0eb11d6c64c962fcc35f596c167c6e38045d6cb8/skills/sepia-review/SKILL.md)|诊断性审阅与直接改写有独立入口；校验实际依赖。|
|[iLearn-Lab/NovelClaw · apps/multiagent/agents/writer_agent.py](https://github.com/iLearn-Lab/NovelClaw/blob/eac2fda3534205f5c70732e653b6f07a575f5ac9/apps/multiagent/agents/writer_agent.py)|读分章生成器的依赖边界：长期记忆检索和静态知识有不同入口；本项目进一步要求工作授权。|
|[danjdewhurst/story-skills · skills/genre-craft/references/fantasy.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/genre-craft/references/fantasy.md)|把规则的可观察边界与代价分开核对；未确定的限制留给创作任务。|
|[danjdewhurst/story-skills · skills/genre-craft/references/horror.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/genre-craft/references/horror.md)|不安可来自期待和观察的差异；节奏可由间隔、反应和信息缺口改变。|
|[danjdewhurst/story-skills · skills/genre-craft/references/mystery-fair-play.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/genre-craft/references/mystery-fair-play.md)|审查读者可见的线索出现时间及其解释空间。|
|[danjdewhurst/story-skills · skills/genre-craft/references/serial-episodic.md](https://github.com/danjdewhurst/story-skills/blob/74dd2f92dd757d4b262085bddccb09ed5a4281d1/skills/genre-craft/references/serial-episodic.md)|检查每单元的问题和整卷连续性；节奏无需每章重复同一套升阶。|

## 文学、叙事与心理资料

|来源和章节|证据适用范围|
|---|---|
|[Start writing fiction: 1.1 Creating characters · OpenLearn - Open University](https://www.open.edu/openlearn/history-the-arts/creative-writing/start-writing-fiction/content-section-1.1)|通过人物欲望和阻碍检视因果，不要求一种固定结构。|
|[Start writing fiction: 1.5 Tips on character creation · OpenLearn - Open University](https://www.open.edu/openlearn/mod/oucontent/view.php?id=1622&section=1.5)|情境、对象和意图可改变说话方式。|
|[Fiction Basics](https://owl.purdue.edu/owl/subject_specific_writing/creative_writing/writers/fiction_writing_basics/index.html)|把故事中的先后关系与实质因果分别记账。|
|[Fiction Writing Basics 2](https://owl.purdue.edu/owl/subject_specific_writing/creative_writing/writers/fiction_writing_basics/fiction_writing_basics_2.html)|分开叙述者、观察者与角色可知范围。|
|[10.1 Motivation - Psychology 2e · OpenStax](https://openstax.org/books/psychology-2e/pages/10-1-motivation)|把内外在动机当作多种可能解释。|
|[10.4 Emotion - Psychology 2e · OpenStax](https://openstax.org/books/psychology-2e/pages/10-4-emotion)|区分生理反应、主观体验和评价；理论之间保留分歧。|
|[12.4 Conformity, Compliance, and Obedience - Psychology 2e · OpenStax](https://openstax.org/books/psychology-2e/pages/12-4-conformity-compliance-and-obedience)|群体信息可影响判断；研究结果不能预言某个角色的选择。|
|[Time · the living handbook of narratology](https://www-archiv.fdm.uni-hamburg.de/lhn/node/106.html)|区分故事持续时间、叙述顺序及叙述篇幅。|
|[Song Dynasty China · Asia for Educators](https://afe.easia.columbia.edu/songdynasty-module/cities-hangzhou.html)|用于核查南宋杭州日常生活的资料入口，第三方引语保留其观察者身份。|
|[4.3 Social Constructions of Reality - Introduction to Sociology 3e · OpenStax](https://openstax.org/books/introduction-sociology-3e/pages/4-3-social-constructions-of-reality)|区分角色集合和角色冲突，避免把现代生活写成同一阶层的经历。|

大学教学材料和叙事理论提供分析视角，教科书解释受样本与情境限制的机制，开源代码仅说明一种实现。灵异、玄幻与无限流条目是本项目的编辑判断；无限流的副本作用域检查借鉴了连载与状态连续性方法，尚无质量提升数据。

## 三级装载与维护

1. [目录](../../runtime/knowledge/catalog.json)记录角色、合法任务、题材、版本、来源哈希、原始方法与许可。编剧方法沿既有总编方向及卷导演计划岗位加载。开发者、未注册角色及不匹配的任务均无条目。
2. [压缩条目](../../runtime/knowledge/methods.json)取岗位与题材交集，默认只把适配的方法及其使用限制交给受托人。没有题材只返回职业知识。题材输入不能指定外部 URL、磁盘路径或其他作品 ID。
3. 已知错误、审核失败、冲突、证据不足或来源变更触发选中条目的[原始方法](../../runtime/knowledge/references/)回读，核对 SHA-256 后进入新任务快照。需要进一步核查原始教材时，按来源索引人工查证。本服务不向模型输入第三方教材。项目内早期示例角色、情节与正文不在知识装载路径内。

Git blob SHA-1 标识上游原始文件。大学页面的 SHA-256 标识该次取到的 HTML 版本，页面模板也可能使其变化。Purdue 的直接请求返回拦截页，所以仅其两项标记为 reviewed-section-text：对公开阅读器取得的标题及紧随其后的原文段落逐行保存，保留 Markdown 四级标题，以 LF 相连并去首尾空白后计算摘要；不把拦截页哈希登记为原文。所有项目方法源文另存本地 SHA-256，整份冻结输入另算 NF-CANON-1 SHA-256。

上游版本变化先复核适用范围、不同意见与许可，再更新本地原文、压缩条目、版本和哈希。新任务采用新快照，旧 Attempt 继续使用冻结输入。职业方法共享不含作品正文、世界事实或角色 BELIEF。动态雇员资格、Skill 授权和任务冻结继续走 Workshop 既有通道。职业目录不产生新的授权。

第一批结束后，以角色和任务类型统计首次有效率、返工和重复问题。挑选同类型失败与成功反例，给候选方法保留原文位置及回归，独立审阅确认后再冻结第二批。六部成书样本只能作描述性对比，无法单凭两批差异归因到某一条知识。
