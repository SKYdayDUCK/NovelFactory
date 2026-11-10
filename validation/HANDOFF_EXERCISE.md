# 开发交接演练：借剑改赠、旧稿并发与取消迟到

> 保留记录：本演练属于原 v2.0 的 D12 借剑改赠夹具和 strict 接口。R1 的 D20 借改赠另见 [A–H 验收](../docs/15_并发压测与端到端验收.md)，两套夹具不混用。下文 23 个工具是原目录数量，当前目录为 28 个；旧 strict 契约继续适用，新增接受资格与查询机制见 [R1 契约说明](../contracts/R1_README.md)。本演练始终是纸面交接，不是业务测试通过记录。

> 演练日期：2026-11-10。性质：阅读设计包、字段核对和纸面推演；没有实现业务代码，没有运行真实 PostgreSQL、Agent Host、模型或生产系统。下列验收项是待实施案例，不是已通过的生产测试。

## 1. 任务解释与使用资料

按 [nf-kernel-change](../.agents/skills/nf-kernel-change/SKILL.md) 和 [nf-pipeline-change](../.agents/skills/nf-pipeline-change/SKILL.md) 执行。读取根规范、docs03/04/06/07/08/09、核心 Schema、工具及运行时 catalog，并交叉核对 docs01/02/11/12。报告字段的交接细化见 [report_templates.md](../contracts/report_templates.md)。

采用 v1.1 [青霜剑固定测试世界及 T05](../baseline/v1_1/09_压力测试与端到端案例.md)：D12 借剑已经是正式世界事实；D30 师父许诺 D50 转赠也已经发生，另一名写手正在呈现这一事件。D12/D30 是故事日期，不是数据库版本或章节位置。实际生产输入未给出 UUID、作品日历和作者批准，因此下文使用符号引用，不能直接复制为请求 JSON。

| 符号 | 含义 |
|---|---|
| `W_old` | 修订开始时解析出的确切 canon WorldRef |
| `W_repair` | REPAIR 分支当前候选 WorldRef，随该分支接受产生新版本 |
| `W_new` | 完成重建并准备推广的精确候选 WorldRef |
| `E12_old/E12_new` | 相同 occurrence_id、不同 event_version_id 的借剑/赠剑记录 |
| `A30_old` | 写手基于 W_old 生成的不可变 D30 草稿 ArtifactRef |
| `T30/G30` | D30 写作任务与原 generation |
| `T_late/G_old` | 已取消 worker 对应的任务与旧 generation |

若真实任务中 D30 许诺还只是未来计划，不能套用“已有发言”的分支：它应保留为计划或新发言候选，经裁决/接受后才成为发生。若 D12 只是正文用词变化而明确不改变转让含义，则先做表达对账；本演练按作者要求确实把已接受的借用改成所有权赠与处理。

**关键判断：这是 Retcon。** 未发布草稿不会使已接受 D12 世界事件恢复成可随意丢弃的草案。旧正式世界、旧文本与工作流尝试都保留；修复分支推广及新正文接受是不同步骤。[04](../docs/04_事件溯源与版本体系.md) [07](../docs/07_剧情与文学生产系统.md)

## 2. 应调用的服务顺序

下面是应用服务顺序，不意味着运行时 Agent 能直接调用所有内部方法。普通 Agent 只提交建议；控制面凭实际 grant 执行接受、取消和推广。不存在“总编”这个角色名称自动授予权限的规则。

| 步骤 | 服务与现有入口 | 输入和输出 | 必须成立的边界 |
|---|---|---|---|
| 1 固定输入 | TaskService、ContextService；`task_get/context_get` | 固定 W_old、任务代际、D12/D30 事件与正文引用、规则和策略 | 不能在过程中央把 main 别名重新解析成另一版本 |
| 2 分类差异 | NarrativeService 内部对账；必要时 `artifact_put/draft_submit` | 保存作者改稿与原文证据；将借改赠识别为 HISTORICAL_CONFLICT/历史变更请求 | 保存候选不写正式 owner，不撤销原借剑 |
| 3 读取与影响 | WorldQueryService.query_state/query_history；ImpactService.analyze_change | 查询相关权属、占有、生命周期、D12 认知、D30 发言和 D50 计划；形成影响候选 | 角色视图仍按时点和权限裁剪；全文/索引缺失不能证明没有依赖 |
| 4 准备修复 | VersionService.prepare_retcon，使用 PrepareRetconRequest；普通角色可先 `change_propose` | 固定旧 canon、原变化包、保护范围与最早影响点；生成 RetconPlan 并创建/绑定一次 REPAIR | 重试复用该命令的修复计划；不再额外创建第二条同用途分支；旧 canon 暂不改变 |
| 5 重建候选 | VersionService、领域投影器、WorldSchedulerService；语义任务由 TaskService 调度 | 从相容切面重建；明确选择 E12_new，复核后续事件、认知、过程及覆盖 | 不对当前世界先反向扣回再正向赠出；重放已选结果不重新调用角色模型 |
| 6 接受修复分支内容 | ReviewService 内容绑定审批；WorldCommitService.accept_world | 新的 REPAIR ContextBundle、目标分支 WorldChangeSet、AcceptWorldRequest → CommitReceipt | 原子记录选择、关键投影、推进/覆盖、失效屏障和 outbox；只是该分支接受 |
| 7 核验推广包 | VersionService.compare_branches、ImpactService、可信检查器与 ReviewService | 最终 W_new 的 ImpactReport、RevalidationReport；D30 语义决议；RETCON Approval | 检查报告实际证据、清单/规则/registry/投影和保护范围；不能只看报告引用存在 |
| 8 推广 | VersionService.promote_candidate，使用 PromotionRequest | 旧 canon/head 与候选 head 双端核验后切换 canon 指针 | 短事务 CAS；同时写推广审计、最小失效屏障和 outbox；不再次执行赠剑效果 |
| 9 处理依赖 | ImpactService、TaskService、MemoryService、PlanningService | 受影响任务失效/新代输入，摘要和索引更新，必要的局部重规划 | 同步屏障先保护接受资格；细依赖展开可以异步；旧历史查询仍有效 |
| 10 重新对账与编辑 | ContextService → 写作/证据/审核任务 → NarrativeService、ReviewService | 新 SceneContract、必要的改稿或复用、AlignmentReport、内容绑定审核 | 重新接受正文只写叙事与证据映射，不重放世界效果 |
| 11 内部交付 | ReleaseService，若本任务范围继续到交付才执行 | 新世界、新正文及编排的发布清单 | 原未发布稿不等于自动发布；本演练不包含外部发送 |

第 4 步中的原变化包可能仍固定 W_old。VersionService 要生成有来源关系的目标 REPAIR 变化包和任务上下文，不改写原包，也不让接受器暗中替换 `input_world_ref`。RetconPlan 保存最初候选基线，最终报告再绑定 W_new；两者通过修复提交来源链关联。[报告契约](../contracts/report_templates.md)

### 2.1 D12 后的世界与认知检查

按该测试世界的制度，借出与盗窃均不转移所有权。新历史的 D13 应是主角所有、主角占有；旧历史的 D13 仍是师父所有、主角占有。若后续 D20 盗窃及 D31 毁坏经复核仍保留，D20 后实际占有者改变但新历史的所有者仍是主角，D32 仍为已毁坏实例，活跃占有/使用不适用。不能修订一次 D12 就让剑在 D32 复活，也不能把最后确认所有者和毁坏后可能存续的权利混为一项。[03](../docs/03_数据模型与知识图谱.md)

D12 参与者是否当时已理解赠与，须重建相应观察和信念依据。没有接触此事的反派不因作者编辑获得新知识；D30 角色也不会从同一 WorldRef 中自动读到 D31 的未来事实。旧版本的认知保持可查。

### 2.2 D30 许诺不能自动删除

“师父说过将来赠剑”与“师父当时确实仍拥有可赠的剑”是不同命题。先保留发言发生身份，再检查它在新历史中属于冗余承诺、误解、特定表达，还是作者确实要修订的台词。具体选择需要证据与编辑决议，不能由 owner 变化自动否认已经说过的话。[v1.1 T05](../baseline/v1_1/09_压力测试与端到端案例.md)

若只改变表现方式而发言含义未变，可保留原事件版本并改正文。若改变实际许诺内容或取消发言，需在 REPAIR 中显式 REPLACE/DEACTIVATE，连同观察、后续计划和正文一起复核。D50 未来赠与安排不因 D30 发言已经发生就成为正式事实。

## 3. 两条并发支线怎样处理

### 3.1 写手提交基于旧 WorldRef 的 D30 草稿

先分开“收件”和“可用于新正式正文”。`A30_old` 不因版本过期被销毁，也不通过把 JSON 里的 WorldRef 改成 W_new 获得资格。

| 到达时刻/状态 | 合法处理 |
|---|---|
| 修复尚未推广，T30/G30 仍有效 | 可以在旧输入上保存结果或旧分支草稿。W_old 仍是有效历史；该稿不自动属于正在准备的新历史 |
| 推广已发生，相关依赖已改变 | 保留候选/旧版本来源；当前接受返回 DEPENDENCY_STALE，旧批准若不匹配返回 APPROVAL_STALE；不得让异步索引尚未更新成为漏过屏障的理由 |
| T30 同时已被换代或取消 | 旧信封不能更新当前任务槽位，按 STALE_GENERATION 拒绝其当前资格；原对象仍可隔离保留 |
| 草稿一部分与改赠无关 | 可复用文字字节、风格锚点及有效旧证据；新场景契约、依赖和审核须重新建立，不修改原任务记录冒充新输入 |
| 旧稿在推广前已进入旧正文版本 | 保留旧叙事版本；推广时阻断其未经复核直接进入新 narrative/release，不撤销世界事件来“配合”正文 |
| D30 草稿含真正新增世界效果，且它先在旧 canon 接受 | 旧 canon head 改变；修复 promotion 必须 CAS 失败，比较新增历史后重新准备，不丢弃先前已接受结果 |

本例的 D30 与 D12 权属、认知和承诺有实际联系，不能声称是无关旧稿。MVP 可采用保守重新生成输入/重审；P3 的精细自动重验证不是此处可以直接假定已经具备的能力。即便最终正文一字未改，也须用新资格证明它在 W_new 仍合法。

### 3.2 已取消 worker 迟到

授权控制面的 TaskCancelRequest 必须区分控制任务的 `task_id/generation` 与被取消任务的 `target_task_id/expected_generation`。该请求不包含 context_bundle_id：任务可能在上下文建立前就被取消；控制身份仍由受信通道和当前 grant 核验。取消与正式接受使用一致的任务/授权锁顺序，撤销当前接受资格后再尝试停止外部执行。

1. **取消先线性化，结果后到：** 可信网关可保存原始返回供审计；TaskResult 的旧 generation 不可成为当前结果，世界/正文接受同样拒绝。普通 worker 不能靠 `artifact_put` 或改 generation 绕过这一点。
2. **领域接受先成功，取消后到：** 取消不撤销已接受世界或正文。若原请求只是丢回执，重复同一命令返回其有权读取的原 CommitReceipt，而非重新执行或假称从未成功。
3. **供应商调用回执未知：** 保留 UNKNOWN 与预算占用，先对账。技术超时、取消和迟到不改变人物的故事时刻，不自动写成人物沉默、失败或晚到。

TaskReceipt 当前枚举没有“已取消旧结果成为有效收件”的状态；拒绝通过已有 ErrorEnvelope 表达。审计归档与合法任务结果是不同记录。终态任务再次取消的具体回复由 TaskService 状态实现确定，但不可改变上述领域不变量。[04](../docs/04_事件溯源与版本体系.md) [06](../docs/06_Agent编排与管理.md) [08](../docs/08_MCP与工具接口.md)

## 4. 可以复用什么，必须失效什么

这里的“失效”是指不能未经复核用于新版本，不是删除所有旧内容。

| 内容 | 处理 |
|---|---|
| 旧 canon 事件、旧版本投影/快照、旧正文与批准 | 保留原身份和来源；旧 WorldRef 查询受当前权限控制继续有效 |
| D12 之前的相容快照 | 可作为重建基础；先核验选择清单、规则和投影器相容性 |
| 包含旧 D12 效果的快照 | 对新历史失效；不能因为 branch_seq 较小就复用 |
| D12 借剑版本与基于它的权属/认知投影 | 用新事件版本/记录选择重建；不原地修改旧版 |
| D20/D31 等后续发生 | 先复核前提、资源与认知；成立则保留发生身份，受影响版本或解释另行处理 |
| D30 许诺发生 | 不自动删除；语义决定保留、修订或停用，随后对账 |
| D30 旧文本、未受影响的语言段落 | 可作为候选字节和编辑参考复用；新采用资格必须重建 |
| D12/D30 SceneContract、事件对账与批准 | 受改变的世界/认知/内容影响即失效；不能只替换引用中的 revision |
| D50 赠剑计划、以师父所有权为前提的依赖 | 触发局部重规划；计划变化不删除 D30 发言 |
| 新历史用到的相关摘要、人物文学解释和伏笔档案 | 更新或重审原始证据、范围及适用性；不能拿旧摘要贴新 WorldRef |
| 缓存与检索索引 | 旧键可服务旧版本；新键按权限、世界/叙事版本和方法隔离，索引滞后回权威源 |
| 已取消/换代的任务资格与旧审批 | 不复用为当前资格；保留历史尝试和旧审批用于审计 |
| 无关地区/人物/场景的任务 | 没有实际依赖且当前策略允许时继续；不因一个向量索引重建冻结全厂 |

## 5. 本演练实际需要的 Schema 字段

字段名取自交付中的核心 Schema；不把逻辑简称冒充已有 JSON 字段。报告载荷的新增字段明示定义在 report_templates.md，后续再实现机器类型。

| 契约 | 必需字段及本例用途 |
|---|---|
| WorldRef / StoryPoint | `work_id, branch_id, world_revision_id` 与 `tick, phase` 分别固定历史和故事点；D12 日历映射来自作品配置 |
| ArtifactRef / EventRef | `artifact_id, artifact_revision_id, sha256` 与 `occurrence_id, event_version_id`；同一发生改版本，不另造一次赠剑 |
| WorldChangeSet | `schema_version, proposal_id, input_world_ref, event_candidates, event_operations, record_operations, adjudication_refs, process_advances, trigger_evaluations, dependency_fingerprint` |
| EventSelectionOperation | 本例 `operation=REPLACE`、原 `occurrence_id`、`prior_event_version_id=E12_old.event_version_id`、`selected_event_version_id=E12_new.event_version_id`；两个版本值实际必须是 UUID |
| AcceptedEvent / NormalizedEffect | 新 event_version 保留 occurrence 身份，给出 `occurrence_time`、`effect_group_id, representation_id, ruleset_ref, adjudication_ref, effects` 等原契约必填项；所有权变化用 RELATION_SET 的 `subject_id,predicate,object_id,value_status,effect_key` 表达，谓词仍受领域规则校验 |
| PrepareRetconRequest | `schema_version,task_id,generation,context_bundle_id,expected_canon_world_ref,changeset_ref,earliest_impact_point,protected_release_ids,reason,dependency_fingerprint,idempotency_key` |
| PromotionRequest | `schema_version,task_id,generation,context_bundle_id,expected_canon_world_ref,candidate_world_ref,retcon_plan_ref,impact_report_ref,revalidation_report_ref,approval_refs,policy_version,dependency_fingerprint,idempotency_key` |
| Approval | `approval_id,subject_type,subject_sha256,principal_id,policy_version,dependency_fingerprint,disposition,recorded_at,conditions`；批准由服务器记录，RETCON 内容哈希绑定规则见报告契约 |
| AcceptWorldRequest | `schema_version,task_id,generation,context_bundle_id,expected_world_ref,proposal_ref,approval_refs,dependency_fingerprint,idempotency_key`；这里目标是当次 REPAIR head |
| DependencyManifest | `world_ref,policy_version,rule_version,scope_versions,artifact_refs,queries,unresolved_scopes`；包括相关负向查询，不只保存成功命中的行 |
| SceneContract | `input_world_ref,event_refs,story_interval,entry_commitments,exit_commitments,pov_actor_id,narrative_revision_id,arrangement_version,reader_cut_ordinal,dependency_fingerprint` 等既有必填项 |
| DraftSubmitRequest | `task_id,generation,context_bundle_id,scene_contract_ref,draft_ref,world_ref,alignment_ref,dependency_fingerprint,idempotency_key` |
| TaskResult | `schema_version,result_id,task_id,generation,attempt_id,context_bundle_id,dependency_fingerprint,status,artifacts,safe_summary`；TaskSpec 自身没有 attempt_id 字段，不能直接给它补该键 |
| TaskCancelRequest | `task_id,generation,target_task_id,expected_generation,reason,idempotency_key`；不要求尚未建立的 context_bundle_id |
| ErrorEnvelope / CommitReceipt | 使用现有错误字段与已存回执；不把传输 request_id 当业务幂等键，也不把 TaskReceipt 当世界提交回执 |

参考格式正例：[修订变化包](../contracts/examples/retcon_changeset.json)、[准备修订](../contracts/examples/retcon_request.json)、[推广](../contracts/examples/promotion_request.json)。这些是 Schema 格式夹具；UUID/哈希不证明已有对应数据库记录，不能据此声称业务前置条件已通过。

## 6. 最小验收案例

以下 11 项足以覆盖本交接的主要失败边界。前 10 项需要实际服务和相应 PostgreSQL/工作流测试；第 11 项包含不能由纯工程断言替代的文学判断。

| ID | 安排 | 必须观察到的结果 |
|---|---|---|
| H01 旧新历史 | 固定同一 D13 查询 W_old 与 W_new | 旧 owner=师父、新 owner=主角；占有者均为主角；旧引用与证据未被覆盖 |
| H02 不重复效果 | REPLACE E12；重建、fork 与同命令重试 | 每个构建只选择一套有效效果；不出现第二次转移，重复接受返回原回执 |
| H03 发言与未来 | D30 许诺已接受；D50 仍是计划 | 不自动删除 D30，也不把 D50 赠与提前当事实；保留或修订有具体决议 |
| H04 认知隔离 | 重建 D12 参与者、无接触反派及 D30 角色上下文 | 参与者依证据更新；无接触者不获编辑通知；D30 不自动知道 D31 |
| H05 旧写手结果 | 推广后提交 A30_old 与旧依赖，索引失效作业尚未运行 | 候选可归档，新正文接受被同步屏障/依赖检查阻断；不能仅换 WorldRef 通过 |
| H06 取消竞争 | 两独立连接交错取消与结果/接受 | 取消先则旧代拒绝；接受先则结果保持；重试不再次施加，故事时间不动 |
| H07 双端 CAS | 重建期间旧 canon 或候选 head 继续变化 | promotion 拒绝，正式指针不切；保存修复内容并重做必要比较/审批 |
| H08 伪报告/陈旧批准 | 报告只有 PASS 字样，或更换 candidate/规则/registry/报告引用 | 无真实检查证据或输入绑定不匹配时不得推广；不能复用旧 RETCON 批准 |
| H09 修复中断 | 重建后、接受中、推广前分别注入失败 | 旧 canon 仍完整；正式接受与屏障原子；从存档恢复，不靠重新抽模型重演 |
| H10 文本与世界分界 | 改写/删除交剑对白、接受新正文、内部冻结 | 世界效果不重放；删文不撤销赠与；新正文与旧世界不会混装为通过的发布清单 |
| H11 D30 语义和衔接 | 比较保留、解释性改写、实际修订台词三类方案 | 编辑依据角色认知、声音、关系与读者承诺作决定；保存证据与异议，不用单一文学分数决定 |

继承关联：T05、T08、S08，以及任务旧代、审批失效、晚提交、同步失效屏障与恢复类 F 用例；新增案例见 [11](../docs/11_研发路线与测试计划.md)。Schema 正例通过不替代 H01–H11。

## 7. 交接中发现的缺口与修正状态

| 发现 | 已作修正/剩余边界 |
|---|---|
| 原 WorldChangeSet 只有事件载荷，没有明确的事件版本替代/停用操作 | 已补 EventSelectionOperation 与 event_operations，并明确候选存在不等于效果选择 |
| 原 prepare_retcon/promote_candidate 只有服务说明，没有请求 Schema | 已补 PrepareRetconRequest、PromotionRequest；推广仍是受权控制面入口 |
| 只引用报告 ArtifactRef，无法交代报告内部必须核验什么 | 已补 [四种报告字段契约](../contracts/report_templates.md)，包括两端版本、选择清单、规则/投影/registry/策略、覆盖、阻塞与实际证据；具体 Pydantic/机器 Schema 和服务执行尚未实现 |
| 共享运行时技能要求调用角色上限之外的工具 | 已修复四种受限模式，见下节；没有扩大角色权限 |
| KnowledgeService、RepresentationService 的目录落点缺失 | docs02 已补对应应用文件落点 |
| 最终正文接受尚无单独的公共机器请求 DTO | 责任、输入和门禁已由 docs07、DraftSubmitRequest、AlignmentReport、Approval 固定；P2 实现内部 DTO。不能把 draft_submit 返回当作正式接受或虚构不存在的 narrative_accept MCP 工具 |
| 实际作者取舍/真实状态输入缺失 | 本任务是合成演练；D30 的文学选择、真实 UUID、授权与已发布保护范围仍须在实际作品任务中取得。不能由设计包替作者作决定 |

当前设计足以指导开发 Agent 按明确的端口、字段和门禁实施本流程。它尚未证明语义重验证完整、文学选择有效或并发实现正确；这些必须分别由 P1 数据库实验与 P2 实际短卷验证。

## 8. 工具目录与运行时映射核对

核查了 docs01/02/08/11/12、`contracts/tool_catalog.json`、`runtime/roles/catalog.json` 和 `runtime/skills/catalog.json`。下面逐项表格由本轮实际文件提取，工具名以 docs08 的“工具能力目录”为范围，协议 profile 名不算工具。

| 工具 | 机器目录服务 | 普通运行时角色请求数 | 文档/Schema 对应 |
|---|---|---:|---|
| `context_get` | ContextService | 10 | 名称一致；输入/输出 Schema 引用已定位 |
| `world_query` | WorldQueryService | 8 | 名称一致；输入/输出 Schema 引用已定位 |
| `world_history` | WorldQueryService | 8 | 名称一致；输入/输出 Schema 引用已定位 |
| `graph_query` | WorldQueryService | 5 | 名称一致；输入/输出 Schema 引用已定位 |
| `memory_search` | MemoryService | 8 | 名称一致；输入/输出 Schema 引用已定位 |
| `evidence_read` | ContextService | 10 | 名称一致；输入/输出 Schema 引用已定位 |
| `plan_get` | PlanningService | 3 | 名称一致；输入/输出 Schema 引用已定位 |
| `plan_propose` | PlanningService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `action_propose` | AdjudicationService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `advance_prepare` | WorldSchedulerService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `adjudication_propose` | AdjudicationService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `artifact_put` | NarrativeService | 10 | 名称一致；输入/输出 Schema 引用已定位 |
| `draft_submit` | NarrativeService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `review_submit` | ReviewService | 3 | 名称一致；输入/输出 Schema 引用已定位 |
| `impact_analyze` | ImpactService | 6 | 名称一致；输入/输出 Schema 引用已定位 |
| `branch_create` | VersionService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `branch_compare` | VersionService | 1 | 名称一致；输入/输出 Schema 引用已定位 |
| `change_propose` | VersionService | 2 | 名称一致；输入/输出 Schema 引用已定位 |
| `commit_request` | WorldCommitService | 0（控制面） | 名称一致；输入/输出 Schema 引用已定位 |
| `release_prepare` | ReleaseService | 2 | 名称一致；输入/输出 Schema 引用已定位 |
| `task_get` | TaskService | 3 | 名称一致；输入/输出 Schema 引用已定位 |
| `task_result_submit` | TaskService | 10 | 名称一致；输入/输出 Schema 引用已定位 |
| `task_cancel` | TaskService | 0（控制面） | 名称一致；输入/输出 Schema 引用已定位 |

工具名层面：docs08 的 23 个工具与机器目录逐项一致，10 个角色的 requested_tools 均属于该目录。`commit_request` 和 `task_cancel` 没有授给普通小说角色，符合控制面定位；`accept_world/promote_candidate/release_accept` 是内部方法，不是漏配的普通角色工具。docs01/11/12 使用相同责任边界，docs02 服务矩阵与目录落点相符；`change_propose` 的 VersionService 门面可以委托 KnowledgeService，并不产生第二写路径。

运行时技能最初有四组“工具存在，但该角色无权调用”的交接冲突，已通过受限模式修正：

| 角色/技能 | 修正后的路径 |
|---|---|
| chief_editor / plan-volume | 只读现有计划/事实简报并作取舍；artifact_put + task_result_submit 交方向决议，卷导演接棒 |
| environment_director / adjudicate-actions | 仅指定环境/过程候选；不调用 adjudication_propose，交独立裁决任务 |
| evidence_maintainer / review-consistency | 只交断言、对齐和证据候选；不调用 graph_query/review_submit，交正式审核任务 |
| literary_editor / maintain-memory | 只用获准原文和既有事实证据形成文学解释；不调用 world_query/world_history，缺证据交事实维护任务 |

已回读修正后的技能正文与角色 catalog 的 `restricted_skill_modes`。有效工具仍是角色请求上限与服务端 task grant 的交集；技能文字不能弥补未授权工具。本核对证明文件中的名称和受限模式能够对接，未证明真实 Host 已正确执行这些限制。
