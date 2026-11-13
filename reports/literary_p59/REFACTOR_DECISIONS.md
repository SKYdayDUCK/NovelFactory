# 创作规范改动与来源校验

| 决定 | 原始依据与采取的动作 | 验证、代价与限度 |
|---|---|---|
| 保留十个运行时岗位 | 总编、卷导演的计划取舍和行动者本已有不同权限。Workshop 编剧是组织岗位，可通过获准角色完成相应任务。见[职责矩阵](RESPONSIBILITY_MATRIX.md)。 | 编剧、文笔检查、独立评委均无需额外生产授权。正式编排没有增加审批或调用。 |
| 精确导航 | 角色入口补回 catalog 已注册的 volume_review；研究技能在总编处只移交 MethodNote。内文版号落后和可用模式的差异集中在[角色入口](../../runtime/roles/README.md)解释。 | 运行时以目录和内容摘要判定；入口不改变可见工具。 |
| 六项操作写成短任务卡 | [岗位方法](../../docs/literary_knowledge/TASK_METHODS.md)对应条件化卷纲、角色的有限认知、逐场取景、同声与事实反证、研究来源、跨章编辑。每项都带适用问题或反例。来源和限制见[开源对照](../../research/literary_agents/OPEN_SOURCE_SURVEY.md)。 | 实验中三位写手用同一写作者规范分章，整卷编辑找到了分别写作留下的道具和酬劳差异；需人工或授权任务选择合适卡片。 |
| 沿用现有素材入口 | Host 可将一张卡交给特定任务；NF 可在作品授权下采用风格原则、绑定场景/批次，由创作上下文投递。开发流程入口见 [nf-pipeline-change](../../.agents/skills/nf-pipeline-change/SKILL.md)，来源判读见 [nf-research-evidence](../../.agents/skills/nf-research-evidence/SKILL.md)。 | 探针把 write-scene 的 139 码点方法段绑定为一个现有样例作品的 style principle，通过原生 PostgreSQL、Creative Harness、模型信封记录适配器，所发段落与原文 SHA-256 同为 `4aba5c1ab099b8ea210285989b68f53db79c4672967ae0600302dd38f76e62c4`。这是投递试验，未调用外部推理。未选择的知识模块没有随链接载入。 |
| 回退对运行时正文的直接覆盖 | [实验版本](https://github.com/SKYdayDUCK/NovelFactory/commit/ecee9fc948c509996964552c3e6e5f033d0ebf1f)直接改过六个 Skill 及角色文字。RuntimeCatalog 能读新文件；Creative Harness 还查 `runtime/provenance.json` 中的 Git blob。启用它时 writer 默认路径返回 `RUNTIME_UNAVAILABLE`，相关 PG 集成测试三项失败。 | [修正版本](https://github.com/SKYdayDUCK/NovelFactory/commit/c8781c28e34483a6c6cfb5619fab1f8cf3b45b3d)逐字恢复所有注册源，将核心方法留在上述卡片、索引和两份开发技能里。66 组已注册的“角色 × 模式 × 可用主 Skill”快照、来源及能力集合与基线逐项相同。直接替换默认指令需要单独发行来源清单。 |
| 对“AI 味”只开有证据的问题单 | 中英社区对版本、题材及喜好并不一致。账本、名字等意象作为待检的阅读问题，审读时需要上下文、反解释和修改代价。 | 《借冬衣》中真实工钱决定两名演手是否返场，保留金额。《照相请到背面》里两个数字巧合则被评委指出过于工整。用一张禁词表处理两处会造成误判。 |
| 有限返工与全文评阅 | 保留已有审核预算，编辑统一问题并给出可定位修改。独立评测任务属于文档协议，未注册为运行时新岗位。 | 两部通过后停在第一轮。本轮没有相同题目、模型与预算的旧版对照，证据只能定位具体文本修复，不能估计方法对文学分数的因果增益。 |

## 复核用的版本区别

作品采用实验快照的文学方法。最终可合并版本经来源门禁回退个别受保护文件，相同的方法段落改为按需交付。默认生产 Prompt 仍来自原来源清单。没有把实验快照的读者分数归给未选取任务卡的生产调用。没有更新或伪造 Workshop 指令摘要。
