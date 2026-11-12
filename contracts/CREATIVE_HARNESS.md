# Creative Harness 候选接口契约 1.0

本契约面向 NF 与 Agent Workshop 的实现者和验收者，以 NF 主线 `276820316129d546b0a13118bcbe283b06507795` 为设计基线。S0 交付数据形状、合成向量与校验方法。S1 至 S6 实际接入时，分别以真实服务回执验证权限、状态变化和各项效果。[候选 Schema](creative_harness/creative_harness.schema.json)

## 配置与默认值

| 公共路径 | 默认值 | 启用条件与作用 |
|---|---|---|
| `creative_harness.enabled` | false | 允许本次获准任务使用 H1–H4。关闭时沿现有静态 NF 工作流执行。 |
| `creative_harness.dynamic_agents.enabled` | false | 须先启用总开关，允许 H5 提交有限角色或 Skill 候选。 |
| `creative_harness.adaptive_instructions.enabled` | false | 须先启用总开关，允许 H6 提交指令候选并进入受控对照。 |
| `creative_harness.adaptive_instructions.auto_promote` | false | 须启用总开关与自适应指令。自动策略路径仍需绑定有效政策、授权回执、独立审核和可回退的旧版本。 |

配置记录要求四个显式布尔值。根 Schema 的 `x-default-configuration` 是可验证的完整默认实例。叶节点的 JSON Schema `default` 是注解。运行方缺少整份配置时沿用静态路径；提供配置时整体验证，拒绝部分配置或未知键。动态人员和自适应指令可分别启用。测试记录可在系统外观察关闭状态；默认静态任务的任务图、供应商请求、账本与既有读写入口保持原有语义。

## 六个接口及验收对象

| 能力与阶段 | 记录 kind | 输入、输出和权威接口 |
|---|---|---|
| H1 上下文，S1 | `CONTEXT_PACKET` | 以 `WorldQueryService.context_compile` 已签发的完整 `ContextBundle`、授权 `KnowledgeCut`、角色及 Skill 快照为输入。列出所选证据、选择理由和省略项。角色使用自己的 BELIEF 截面和当时已知材料。 |
| H2 规划，S2 | `PLAN_CANDIDATE` | 以既有计划锚点、冻结世界、前提及保护承诺提出有边界的备选。每条含预期结果、风险和反证。正式采用须经过 `PlanningService.adopt_plan` 或相应的滚动规划审核。 |
| H3 推演，S2 | `SIMULATION_REPORT` | 输入冻结候选和切点，为分支、步数、Token、金额、尝试数与时间设置上限。每个结果带成立条件、不确定性与停止原因。推演的正式世界事件数固定为 0。独立世界裁决仍由 NF 提交。 |
| H4 审核，S3 | `REVIEW_REPORT` | 审核目标固定到具体稿件版本。事实或矛盾判断需提供证据，意见绑定段落偏移及引文摘要，注明严重程度、判断类型、理由和修订要求。工程评审、独立盲读各有独立的数据结构。返回意见及有限返工次数。 |
| H5 动态人员及 Skill，S4 | `ROLE_SKILL_PROPOSAL` | 固定原角色及 Skill、指令工件和目标版本，记录触发证据、源安装和员工任期、作品/分支、任务模式、请求能力、人数、期限与预算。现有 Workshop 组织、分享、冻结和撤权接口负责真实变更。 |
| H6 自适应优化，S5 | `INSTRUCTION_CANDIDATE`、`PROMOTION_DECISION` | 新指令保留父版本、来源、失败样例、预期效果、风险和回退引用。经冻结对照、独立评审和获准的晋级路径后，由正式版本管理入口激活。 |
| 观测与比较，S6 | `EXPERIMENT_FREEZE`、`EXECUTION_LOG` | 冻结条件与案例全集，按 NF trace 关联执行、工程指标、预算账本、Workshop 操作和独立阅读结果。正式作品变动须关联原有 NF CommitReceipt。 |

记录使用 `schema_version` 和 `kind` 分流，禁止额外字段。不同记录通过不可变 `ArtifactRef` 或指定账本的主键相连。候选状态分别写为 `HYPOTHESIS_ONLY`、`ADVISORY`、`PROPOSED_NOT_INSTALLED` 或 `PROPOSED_NOT_ACTIVE`。提交候选时实际调用的准入、调任、模型派发和审核权限由各服务控制。[NF 规划](../src/novel_factory/application/planning.py) · [推演与正式裁决](../src/novel_factory/application/simulation.py) · [全文审核](../src/novel_factory/application/editorial.py) · [Workshop 绑定与冻结](../workshop/src/agent_workshop/reviews_skills.py)

## 解析、授权和冻结

1. `scope.world_ref` 使用 NF 2.0 的 `WorldRef`。解析时一起核查作品、分支、世界修订、任务及代际。H1 的 `context.world_ref/task_id/generation` 须与外层 `scope` 全部相等，角色、时点、策略与 actor_id 须与活动授权一致。每份来源须属于同一已获准的工作切面。权限失效或切面不可解析时停止交付。
2. `ArtifactRef` 固定工件及修订 UUID 和 SHA-256。读取时用已有 NF 工件服务验证类型、内容和可见性。H1 的来源证据须属于已编译上下文许可范围，并按任务类型验证证据跨度。正文区间使用 NF `TextSpan` 的 Unicode 码点及引文摘要；须另验边界、实际文本与 SHA-256。日志保留引用、理由和用量，敏感正文和绑定凭据留在对应服务。
3. `RuntimeSnapshot` 的 `source_commit`、`role_blob`、`skill_blob` 是 Git 对象标识；`runtime_sha256` 是 `RuntimeCatalog.snapshot` 按 NF 规范计算的完整角色、模式及 Skill 快照摘要。执行时复核 `AgentGateway.prepare_input` 和任务内已保存的 `runtime_contract_version`。恢复或重复调用重用原快照和请求键。[RuntimeCatalog](../src/novel_factory/infrastructure/runtime_catalog.py) · [AgentGateway](../src/novel_factory/application/agent_gateway.py)
4. `NFPrincipal` 记录 NF 的 principal、grant 和权限代际。`WorkshopBinding` 记录合法源绑定的 org、installation、员工、任期、授权 epoch、Skill 标识/修订和内容 SHA。H5 候选中的源绑定可指向被改进的现有 Skill。Workshop 在实际委托或冻结时复核当前安装、人员、当前绑定、共享策略、期限及原任务。NF 和 Workshop 使用各自身份与数据库。
5. 细粒度文学评语固定匿名样本、盲分配、阅读回执、维度、1–5 分和对应段落。评审者必须与目标生产者和实质代理分离。正式服务复核独立身份、当前项目授权、读者与分配水印、签署时间和还有效的任期。事实审计需要证据，主观偏好保留意见与理由。独立阅读结论要求至少两位真实独立读者；出现分歧时保留原评分和复核意见。没有独立阅读时记录 `UNPROVEN` 或 `NOT_EVALUATED`。
6. 金额使用 NF 币种、十进制字符串与既有预算 ID。每次可能产生费用的调用都先由 NF 在实际账户上核验共享金额、共享 Token、任务限额和准入；Workshop 委托另受组织预算控制。超限停止。日志只读 `NF_ATTEMPT_LEDGER`，用 `attempt_id/task_id/generation/budget_id` 回查原尝试。无尝试保留零 Token、空账单；未确认账单用 null，`UNKNOWN` 保持原预留，按原尝试对账。回退和幂等重试均保留既有账本。[NF 共享预算](../docs/operations/shared-token-budget.md)

## 冻结试验与晋级

S5 的 A/B 使用 `CONTROL`、`TREATMENT` 两臂，分别锁定候选版本。在同一配置和授权范围内只改变预登记的变量。S6 使用以下三个固定名称。数组顺序定义臂身份；实际展示和执行顺序由冻结的种子、分配表和盲评政策控制。

| S6 臂 | 总开关 | 动态人员 | 自适应指令 | 自动生产晋级 |
|---|---|---|---|---|
| `BASELINE` 原静态系统 | 关 | 关 | 关 | 关 |
| `HARNESS` 新 Harness，动态优化关闭 | 开 | 关 | 关 | 关 |
| `HARNESS_ADAPTIVE` 新 Harness，动态配置与候选优化开启 | 开 | 开 | 开 | 关 |

同一 `EXPERIMENT_FREEZE` 固定作品、输入、案例全集、模型配置摘要、可比较预算政策、量表、盲分配、重复次数与顺序种子。各臂提交全部预登记案例的成功、失败、拒绝和超时结果。报告分别给出工程正确性、审核效果、Token、可对账成本、延迟、返工；文学质量由独立阅读报告支持。A/B 可以验证单项改动，S6 三臂按冻结条件评价系统。工程全绿本身只支持工程结论。

`PROMOTION_DECISION` 区分建议、拒绝、批准与回退。正式批准和回退须提交有权限主体、有效政策、到期时间和授权回执。`POLICY_AUTOMATIC` 路径还需三个相关开关满足依赖。执行方绑定原基线、候选、冻结试验及当时实际部署，在事务中复核当前版本、可见性、独立身份、指标阈值和预算，再调用既有版本管理入口，保存原操作回执。需要文学提升的策略须验证两份有效盲读，`UNPROVEN` 仅可支持不涉及文学提升的判断。实验中的任一臂均关闭自动生产晋级。[版本审核与激活](../src/novel_factory/application/upgrades.py)

## 校验协议

把 `contracts/*.schema.json` 和 `contracts/creative_harness/creative_harness.schema.json` 按绝对 `$id` 装入同一个离线 `referencing.Registry`。使用 `Draft202012Validator.check_schema` 校验文档，再以 `Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())` 对每个向量的 `instance` 校验。两份向量都是 `[{"name": "...", "instance": {...}}, ...]`，全部名称全局唯一。正例逐条无错误，反例逐条至少一个错误。候选 Schema 不参与生产 API 注册。[正例](examples/creative_harness_s0.json) · [反例](invalid_examples/creative_harness_s0.json)

S0 的静态检查覆盖类型、版本引用形状、必填字段、关联系统的不可变标识、开关、界限、互斥状态、臂序及所需证据。跨对象标识相等、授权真伪、摘要内容、实名独立、账实一致、撤权后拒绝和并发世代一致性须用现有服务验真；填写合规 JSON 不能产生授权。示例中的作品、账目、审核及文本引用均为合成，`context_packet_actor` 的角色和 Skill 版本、Git blob、运行时摘要则固定到上述实际源码。任何向量的校验均不需要数据库、创作模型、供应商请求或付费授权。
