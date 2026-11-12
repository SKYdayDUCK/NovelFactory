---
name: plan-volume
description: 在制定全书或卷创作契约、滚动剧情窗口、场景细纲，以及已接受意外导致局部或卷级重规划时使用。
---

# plan-volume

适用角色/模式：volume_director / chief_editor。继承[运行时公共协议](../../roles/README.md)，权限来自当前任务grant；本文件只提供方法。

## 取得输入

WorldRef；过程覆盖和未完成影响；人物/势力各自目标；前卷双结算；叙事问题；保护承诺、预算及允许候选范围。

先核验task_id、generation、context_bundle_id、policy_version与依赖；缺失关键输入时返回具体缺口，不假定“最新”即正确。实际执行先取本任务grant与角色模式；下列工具是该技能各模式的能力并集，只有所属模式明确允许且本次grant包含时才调用。缺少所需能力时，使用当前已有合法材料返回候选或NEEDS_INPUT，交控制面安排具备权限的后续任务，不换身份补权限。

## 按角色模式选择路径

- `volume_director`：在本次grant内查询、准备窗口、比较获准分支并提交计划；可以执行下列完整规划路径。
- `chief_editor`：读取已提供的计划、事实简报和比较报告，只作方向/候选取舍。不得调用 `world_query`、`advance_prepare`、`branch_create`、`branch_compare` 或 `plan_propose`；需要这些结果时输出具体需求，由控制面交卷导演接棒。用 `artifact_put`＋`task_result_submit` 保存 DecisionRecord，不直接创建或接受计划。

卷导演可从读者在开篇等什么倒推一处必需的先行条件。卷尾用人物选择造成的具体变化连接下一卷，记清哪位人物仍不知情。写进已有卷契约或交接说明即可。若只有一种能推动计划的台词或行动，先看人物还能拒绝什么，再决定哪些未来场次应留白。例：师傅拒绝补演，徒弟仍能通过另一次试台决定是否接活。

## 执行

1. 从context_get和plan_get取固定版本，分开事实、角色目标、世界过程、作者弧线意图与普通叙事偏好。
2. 进行全卷可行性检查，确认关键前提、资源、时间和可接受的失败。仅在关键不确定性值得成本时用授权simulation分支预演。
3. 按关键交汇而非章号选择滚动窗口；卷导演有grant时用advance_prepare请求本地调度，总编仅指出需核查的窗口并交接。不自行判断推进已安全。
4. 用typed依赖分析已接受变化：硬前提失效、替代路径、语义风险与文学承诺分别处理。提出少量结构不同的方案并保留真实代价。
5. 卷导演有grant时将选中计划、SceneContract、未采纳候选理由和受影响任务提交plan_propose；总编模式只归档取舍决议并交控制面。需要核心方向裁量时交chief_editor。

## 交付

卷导演输出VolumeContract/PlanRevision或SceneContract；总编输出DecisionRecord和待交接需求。均包括基础版本、条件、风险、候选差异、选择理由和下一阶段输入。未来节点保持计划身份。

保存不可变产物和实际证据，回传当前任务信封。任务收件、世界接受、正文冻结及外部发布不是同一状态。

## 依赖工具

共同：context_get、plan_get、impact_analyze、memory_search、evidence_read、artifact_put、task_result_submit。仅卷导演且有本次grant：plan_propose、advance_prepare、branch_create、branch_compare、world_query。

工具名与错误语义见[08](../../../docs/08_MCP与工具接口.md)。可见工具是本次服务端授权的子集；不得换角色或修改技能文件绕过拒绝。

## 完成与退出

不能因路线失败改判世界，不能让角色预读结局。无可信路线时允许阶段失败、收束或调整目标；已接受历史需正式修订，技术重试不得重抽剧情。

完成规定产物、证据和未决项后结束；无新增证据的重复审美争论不无限返工。

## 按需参考

- [规划、重规划和场景契约](../../../docs/07_剧情与文学生产系统.md)
- [候选采用与修订](../../../docs/04_事件溯源与版本体系.md)

## R1时间与批量上下文

优先使用任务已编译上下文；本角色profile和当次grant允许时，使用context_compile或timeline_query/query_batch减少重复读取。批量只有一个WorldRef与KnowledgeCut，不能逐项换身份/分支。ACTOR只使用BELIEF与合法known_at；计划、预测和承诺不当作目标结果。固定历史未推进返回未求值，不能等到新HEAD后冒充原版事实。遇TASK_REBIND_REQUIRED交控制面新建输入代际；遇真实依赖变化保留候选并定向返工，不自行改base/审批或签发接受证书。
