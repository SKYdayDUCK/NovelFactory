---
name: prepare-world
description: 为小说初始化或扩展世界知识、规则、冗余背景与过程登记；在需要核对历史状态、实体身份、制度资源和新设定影响时使用。
---

# prepare-world

适用角色/模式：world_designer / environment_director。继承[运行时公共协议](../../roles/README.md)，权限来自当前任务grant；本文件只提供方法。

## 取得输入

作品创作约定；精确WorldRef；作者保护范围；地理、力量、文明、历史、势力和资源结构；本次缺口与允许开放的细节。

先核验task_id、generation、context_bundle_id、policy_version与依赖；缺失关键输入时返回具体缺口，不假定“最新”即正确。实际执行先取本任务grant与角色模式；下列工具是该技能各模式的能力并集，只有所属模式明确允许且本次grant包含时才调用。缺少所需能力时，使用当前已有合法材料返回候选或NEEDS_INPUT，交控制面安排具备权限的后续任务，不换身份补权限。

## 执行

1. 用 context_get 确认当前任务，再以 world_query/world_history/graph_query 查询已有实体、规则及历史。需要否定结论时检查coverage，不能把搜索空结果当不存在。
2. 区分已接受背景、未知细节、未来计划和待推进过程。保存冗余设定不等于为其安排持续模型调用；已接受而未出场的设定仍有约束力。
3. 检查结构相互支撑：力量代价、供给、距离、交通通信、制度和势力目标。只展开影响当前判断的缺口，不枚举全部居民。
4. 对新通道、能力、制度或重要历史执行impact_analyze；若改变既有后果，转正式修订提案。
5. 通过change_propose交设定/过程建议，附开放问题、证据、影响和建议验收；产物归档后结束。

## 交付

WorldChangeProposal＋结构覆盖说明＋开放问题；历史查询保留AnswerEnvelope状态和证据，不复制出另一套可写世界。

保存不可变产物和实际证据，回传当前任务信封。任务收件、世界接受、正文冻结及外部发布不是同一状态。

## 依赖工具

world_query、world_history、graph_query、change_propose、impact_analyze、artifact_put、task_result_submit；读取由任务grant限制。

工具名与错误语义见[08](../../../docs/08_MCP与工具接口.md)。可见工具是本次服务端授权的子集；不得换角色或修改技能文件绕过拒绝。

## 完成与退出

普通兼容扩展可在已有授权内完成；关键历史/保护规则冲突给出具体修订方案。不得以作者计划证明事实，不得直接修改canonical记录。

完成规定产物、证据和未决项后结束；无新增证据的重复审美争论不无限返工。

## 按需参考

- [知识、状态和时间语义](../../../docs/03_数据模型与知识图谱.md)
- [过程与推进范围](../../../docs/05_世界模拟与调度引擎.md)

## R1时间与批量上下文

优先使用任务已编译上下文；本角色profile和当次grant允许时，使用context_compile或timeline_query/query_batch减少重复读取。批量只有一个WorldRef与KnowledgeCut，不能逐项换身份/分支。ACTOR只使用BELIEF与合法known_at；计划、预测和承诺不当作目标结果。固定历史未推进返回未求值，不能等到新HEAD后冒充原版事实。遇TASK_REBIND_REQUIRED交控制面新建输入代际；遇真实依赖变化保留候选并定向返工，不自行改base/审批或签发接受证书。
