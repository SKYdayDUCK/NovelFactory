# NovelFactory 技能索引

## 开发与运维

任务匹配后读取对应方法，先固定输入、版本与权限，再按步骤验证输出。长资料见技能末尾引用。

| 技能 | 触发任务 | 关键输出 |
|---|---|---|
| [nf-kernel-change](.agents/skills/nf-kernel-change/SKILL.md) | 世界状态、正式接受、版本、时间、调度和效果变更 | 不变量、候选实现、兼容性及风险验证 |
| [nf-contract-change](.agents/skills/nf-contract-change/SKILL.md) | Schema、DTO、任务或 MCP/API 契约变更 | 同步契约、有效/无效例、调用方迁移 |
| [nf-recovery-check](.agents/skills/nf-recovery-check/SKILL.md) | 备份恢复、重放、投影、丢回执和故障处理 | 隔离演练、前后核验、未恢复项 |
| [nf-research-evidence](.agents/skills/nf-research-evidence/SKILL.md) | 论文/文档核查、技术选型和平台适配 | 可追溯来源、实现映射与验证等级 |
| [nf-concurrency-review](.agents/skills/nf-concurrency-review/SKILL.md) | 统一时间、批量读取、scope守卫、缓存、池与压测变更 | 一致性/负载分层、并发反例、实测与未测边界 |
| [nf-pipeline-change](.agents/skills/nf-pipeline-change/SKILL.md) | 规划、写作、审核与整卷编辑流程变更 | 阶段契约、失败路径、工程/文学验收 |
| [nf-resume](.agents/skills/nf-resume/SKILL.md) | 作者要求通过已绑定的项目 MCP 恢复小说生产 | 获取当前作品状态与实际唤醒数，后台调度器接续 |

## 生产角色

每次任务只选一个注册角色与一个主 Skill，版本和文本随任务冻结。开发方法不会进入角色上下文。每个 Skill 的“适用岗位与输入”“执行”“验证与交接”给出输入、必要步骤、候选输出与检查。上下级走 Supervisor 交接，Workshop 岗位与 NF 角色的映射由组织配置决定。

| 技能 | 主要任务 | 操作边界 |
|---|---|---|
| [prepare-world](runtime/skills/prepare-world/SKILL.md) | 世界知识准备与正式设定提案 | 设定候选不自动成为世界事实 |
| [plan-volume](runtime/skills/plan-volume/SKILL.md) | 动态卷纲与局部重规划 | 未来计划不替代已接受事件 |
| [simulate-character](runtime/skills/simulate-character/SKILL.md) | 角色认知读取和行动提案 | 只读角色获准上下文，不判定自身行动成功 |
| [adjudicate-actions](runtime/skills/adjudicate-actions/SKILL.md) | 语义裁决建议与规则证据 | 输出候选，正式接受仍在内核 |
| [write-scene](runtime/skills/write-scene/SKILL.md) | 场景/章节细纲与正文 | 既有世界效果不重复执行 |
| [review-consistency](runtime/skills/review-consistency/SKILL.md) | 事实、披露、时间和衔接审核 | 提交证据与建议，不自行修订历史 |
| [maintain-memory](runtime/skills/maintain-memory/SKILL.md) | 历史查询、摘要及记忆维护 | 摘要是可追溯派生物，受版本与认知范围约束 |
| [research-literature](runtime/skills/research-literature/SKILL.md) | 文学资料研究与作品风格参考 | 通用方法与作品决策隔离，不套用普遍质量公式 |
| [edit-volume](runtime/skills/edit-volume/SKILL.md) | 整卷结构、衔接与语言编辑 | 事实变化走变更提案；表达编辑不撤销世界 |
| [volume-director-review](runtime/skills/volume-director-review/SKILL.md) | 审核人物行动和剧情偏离 | 引用已接受事件并交代动机、代价及待决项 |
| [review-change-point](runtime/skills/review-change-point/SKILL.md) | 审议变更点及候选未来窗口 | 总编独立批准或退回，需要作者决定时列为待办 |
| [roll-window](runtime/skills/roll-window/SKILL.md) | 起草获准窗口内的重规划 | 保留原参考线及窗口外场次，提交独立审议 |

## 知识与恢复

P6 根据作品绑定的题材、NF 职业与任务选择公开方法，日常读取压缩条目。审核失败或诊断需要时回读带哈希的原始方法卡；本作品私有事实仍取自绑定的 ContextBundle。诊断、输入缺口和候选保留给控制器。修订从带证据的具体问题进入，完成后按相同标准复核。

角色和技能只定义工作方法，实际授权由服务端 grant 签发。文本模型只返回任务契约允许的结果。绑定版本失效时由控制器重新绑定和登记新任务。旧任务继续使用原快照。详见[角色目录](runtime/roles/README.md)、[知识目录](runtime/knowledge/catalog.json)与[文学方法索引](docs/literary_knowledge/README.md)。
