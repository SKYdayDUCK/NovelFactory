---
name: review-consistency
description: 在核查正文事实、角色认知、时间因果、跨章连续性及重要终态，或对抽取结果和作者解释作独立复核时使用。
---

# review-consistency

适用角色/模式：reviewer / evidence_maintainer（仅证据候选）。继承[运行时公共协议](../../roles/README.md)，权限来自当前任务grant；本文件只提供方法。

## 取得输入

精确ArtifactRef/WorldRef；审核维度与范围；场景/叙事约定；读者披露与角色认知边界；关键源证据。

先核验task_id、generation、context_bundle_id、policy_version与依赖；缺失关键输入时返回具体缺口，不假定“最新”即正确。实际执行先取本任务grant与角色模式；下列工具是该技能各模式的能力并集，只有所属模式明确允许且本次grant包含时才调用。缺少所需能力时，使用当前已有合法材料返回候选或NEEDS_INPUT，交控制面安排具备权限的后续任务，不换身份补权限。

## 按角色模式选择路径

- `reviewer`：执行受权审核并在有grant时调用 `graph_query`、`review_submit`。
- `evidence_maintainer`：只整理断言、已有事件关联和问题证据候选；不作正式审定，不调用 `graph_query` 或 `review_submit`。需要图核验或独立评判时列出最小问题和合法证据，用 `artifact_put`＋`task_result_submit` 交真正的审核任务接棒。

## 执行

1. 先独立阅读证据，记录实际原文范围；只读摘要就明确标注。
2. 对关键资源、终态、信息和时间直接查询/核对，避免把抽取器无报错当证明。
3. 区分倒叙、谎言、传闻、误信、不可靠叙述、所有权和持有权；未知或待推进不自动算矛盾。
4. 用issue_kind将问题分为HARD_CONFLICT、SEMANTIC_CONCERN、LITERARY_CHOICE或CHANGE_REQUEST，category另记主题、severity另记严重性；提供原文与事实锚点、反解释及最小修订范围。
5. reviewer有grant时用review_submit交问题或no_change，并复审原issue；evidence_maintainer只归档证据候选和待审核问题，不把其候选标为已通过。均保护合法多声部与有意留白。

## 交付

reviewer输出ReviewReport及阅读覆盖；evidence_maintainer输出断言/关联/问题证据候选及覆盖说明，交独立审核者。每个重要判断有具体版本证据；允许未定、无修改和保留分歧，不强行产出固定问题数。

保存不可变产物和实际证据，回传当前任务信封。任务收件、世界接受、正文冻结及外部发布不是同一状态。

## 依赖工具

共同且受视图限制：context_get、world_query、world_history、memory_search、evidence_read、artifact_put、task_result_submit。仅reviewer且有本次grant：graph_query、review_submit。

工具名与错误语义见[08](../../../docs/08_MCP与工具接口.md)。可见工具是本次服务端授权的子集；不得换角色或修改技能文件绕过拒绝。

## 完成与退出

硬冲突由服务门禁确认，不能模型投票豁免；审美分歧交负责编辑。不能以全知状态指责合理误信，不得越权读秘密后回传给角色。

完成规定产物、证据和未决项后结束；无新增证据的重复审美争论不无限返工。

## 按需参考

- [审核与有限返工](../../../docs/06_Agent编排与管理.md)
- [对账分类](../../../docs/07_剧情与文学生产系统.md)
- [T/S/L/F验收](../../../docs/11_研发路线与测试计划.md)

## R1时间与批量上下文

优先使用任务已编译上下文；本角色profile和当次grant允许时，使用context_compile或timeline_query/query_batch减少重复读取。批量只有一个WorldRef与KnowledgeCut，不能逐项换身份/分支。ACTOR只使用BELIEF与合法known_at；计划、预测和承诺不当作目标结果。固定历史未推进返回未求值，不能等到新HEAD后冒充原版事实。遇TASK_REBIND_REQUIRED交控制面新建输入代际；遇真实依赖变化保留候选并定向返工，不自行改base/审批或签发接受证书。
