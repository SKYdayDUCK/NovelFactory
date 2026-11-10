---
name: maintain-memory
description: 在生成或更新事实/角色/文学摘要、对齐正文事件、维护人物弧线解释、关系不对称、原文锚点及跨卷未竟后果时使用。
---

# maintain-memory

适用角色/模式：evidence_maintainer / literary_editor。继承[运行时公共协议](../../roles/README.md)，权限来自当前任务grant；本文件只提供方法。

## 取得输入

固定源清单和覆盖范围；WorldRef与允许plane；精确正文ArtifactRefs；更新触发及旧摘要/档案依赖。

先核验task_id、generation、context_bundle_id、policy_version与依赖；缺失关键输入时返回具体缺口，不假定“最新”即正确。实际执行先取本任务grant与角色模式；下列工具是该技能各模式的能力并集，只有所属模式明确允许且本次grant包含时才调用。缺少所需能力时，使用当前已有合法材料返回候选或NEEDS_INPUT，交控制面安排具备权限的后续任务，不换身份补权限。

## 按角色模式选择路径

- `evidence_maintainer`：有本次grant时用 `world_query`/`world_history`核验事实和认知来源，再生成对应摘要候选。
- `literary_editor`：仅从已获准ContextBundle、现成事实证据和可读原文构建文学档案；不得调用 `world_query`/`world_history`自行扩大为全知查询。缺少事实核验时，把问题与已有证据交给事实维护任务；先保留解释为未定，不补造世界事实。

## 执行

1. 通过evidence_read回读原始材料，区分事实、信念、发言、未来意图和编辑解释。
2. 识别已有发生身份；同名/同动词不能强合并，不确定则保留候选。
3. 压缩时保留关键选择与代价、消息时间、关系双方不同认知、未完成过程和原文锚点。摘要的摘要仍追溯到原始来源。
4. 文学档案区分已接受私有决定、作者弧线意图、解释及读者反应假设；给解释保留反证和适用范围。
5. 证据维护模式保存相应SummaryCandidate/LiteraryMemoryCandidate，文学编辑模式只保存获准文学解释候选；均附源依赖、交任务服务审核。事实缺口交后续核验任务；失效旧稿不能只换revision继续使用。

## 交付

带证据和覆盖范围的摘要/档案候选，或事件/断言对齐候选；原文锚点按hash和半开字符区间固定。

保存不可变产物和实际证据，回传当前任务信封。任务收件、世界接受、正文冻结及外部发布不是同一状态。

## 依赖工具

共同：context_get、memory_search、evidence_read、artifact_put、task_result_submit。world_query/world_history仅供evidence_maintainer且本次grant包含时使用；literary_editor使用现有合法来源，不调用这两个工具。

工具名与错误语义见[08](../../../docs/08_MCP与工具接口.md)。可见工具是本次服务端授权的子集；不得换角色或修改技能文件绕过拒绝。

## 完成与退出

不直接写世界事实、不把未召回当遗忘、不把未来成长当已实现、不以单个情绪词归零经历。缺证据则留空或未定，不填心理学套话。

完成规定产物、证据和未决项后结束；无新增证据的重复审美争论不无限返工。

## 按需参考

- [摘要、档案和失效](../../../docs/09_长期记忆与检索.md)
- [双结算与原文覆盖](../../../docs/07_剧情与文学生产系统.md)

## R1时间与批量上下文

优先使用任务已编译上下文；本角色profile和当次grant允许时，使用context_compile或timeline_query/query_batch减少重复读取。批量只有一个WorldRef与KnowledgeCut，不能逐项换身份/分支。ACTOR只使用BELIEF与合法known_at；计划、预测和承诺不当作目标结果。固定历史未推进返回未求值，不能等到新HEAD后冒充原版事实。遇TASK_REBIND_REQUIRED交控制面新建输入代际；遇真实依赖变化保留候选并定向返工，不自行改base/审批或签发接受证书。
