---
name: adjudicate-actions
description: 当多个角色行动相互影响、共享资源需要联合结算，或确定性规则留有语义后果缺口、宏观结果需要兼容细化时使用。
---

# adjudicate-actions

适用角色/模式：adjudication_advisor / environment_director（仅指定过程）。继承[运行时公共协议](../../roles/README.md)，权限来自当前任务grant；本文件只提供方法。

## 取得输入

裁决模式：共同WorldRef、完整相关意图集合、规则版本、故事时窗与阶段、资源和影响覆盖、既有效果与必要观察。环境模式：只取本任务指定过程的WorldRef、时窗、规则及已获准证据，不要求取得角色密封意图或全局裁决资料。

先核验task_id、generation、context_bundle_id、policy_version与依赖；缺失关键输入时返回具体缺口，不假定“最新”即正确。实际执行先取本任务grant与角色模式；下列工具是该技能各模式的能力并集，只有所属模式明确允许且本次grant包含时才调用。缺少所需能力时，使用当前已有合法材料返回候选或NEEDS_INPUT，交控制面安排具备权限的后续任务，不换身份补权限。

## 按角色模式选择路径

- `adjudication_advisor`：在受权联合裁决任务中读取完整冲突组，生成裁决候选；有grant时才调用 `adjudication_propose`。
- `environment_director`：只处理任务指派的环境/过程范围和已提供合法证据，不读取完整密封人物意图、不决定全组胜负。输出 EnvironmentProposal，用 `artifact_put`＋`task_result_submit` 交控制面转给独立裁决任务；不得调用 `adjudication_propose`。

## 执行

1. 裁决模式核查参与集合、入站过程与时间前提；环境模式只核查指定过程的前提并列出可能影响。缺失高影响信息时返回待定，不按队列先后选胜者。
2. 将可由确定性规则处理的约束交服务校验；禁止规则例外由自由文本悄悄生成。
3. 裁决模式从共同起点判断排他结果、实际消耗与失败后果；环境模式只提出范围内过程变化，不对人物竞争作整体裁决。顺序不明且影响结果时请求细化/联合处理，不任意拓扑排序定历史。
4. 对宏观已接受历史只提出等价细化与呈现关联；新损耗或新消息改变效果时标为修订。
5. 裁决模式有grant时用adjudication_propose交后果、证据和未决项；环境模式只用artifact_put和task_result_submit保存EnvironmentProposal并交接。任何正式反馈给角色前均由ContextService重新裁剪。

## 交付

裁决模式输出AdjudicationProposal，包含行动结论、共同效果候选、因果/时间依据、资源与观察变化和未决项；环境模式输出指定过程的EnvironmentProposal，不声称已完成联合裁决。建议与正式接受分开。

保存不可变产物和实际证据，回传当前任务信封。任务收件、世界接受、正文冻结及外部发布不是同一状态。

## 依赖工具

共同且受范围限制：context_get、world_query、world_history、graph_query、evidence_read、impact_analyze、artifact_put、task_result_submit。adjudication_propose仅供adjudication_advisor且本次grant包含时使用。

工具名与错误语义见[08](../../../docs/08_MCP与工具接口.md)。可见工具是本次服务端授权的子集；不得换角色或修改技能文件绕过拒绝。

## 完成与退出

不能替角色选目标、为文学结局偏袒、重抽已接受结果或直接执行world commit。无法证明规则/时间相容时返回BLOCKED及最小缺口。

完成规定产物、证据和未决项后结束；无新增证据的重复审美争论不无限返工。

## 按需参考

- [调度与裁决接口](../../../docs/05_世界模拟与调度引擎.md)
- [effect与接受协议](../../../docs/04_事件溯源与版本体系.md)

## R1时间与批量上下文

优先使用任务已编译上下文；本角色profile和当次grant允许时，使用context_compile或timeline_query/query_batch减少重复读取。批量只有一个WorldRef与KnowledgeCut，不能逐项换身份/分支。ACTOR只使用BELIEF与合法known_at；计划、预测和承诺不当作目标结果。固定历史未推进返回未求值，不能等到新HEAD后冒充原版事实。遇TASK_REBIND_REQUIRED交控制面新建输入代际；遇真实依赖变化保留候选并定向返工，不自行改base/审批或签发接受证书。
