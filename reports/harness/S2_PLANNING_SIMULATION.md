# S2 候选剧情与隔离推演

卷导演在现有 PLAN 切面下调用 `nf_harness_plan_propose`。只有已启用 Harness、持有有效租约、冻结 Skill 允许 plan_propose、具备 PLAN_READ 与 ARTIFACT_WRITE、已绑定真实计划节点的任务才能提出候选。任务 ID、代际、工作者身份和世界切点由服务绑定。调用方提供已选计划引用、一个到八个已授权节点、触发原因、场景窗口和演算额度。普通场景产生一条路径，动机冲突或不可逆后果可展开多个有出处的选择。每项保留目标、前提、后果、风险、反证引用和选择理由。发布承诺和历史节点沿用原计划约束。

候选返回 `candidate_ref` 和每个选项的 NF 原生计划引用。独立获准操作者可以拿该引用走已有滚动计划审核。正式采用通过 PlanningService 的既有预期版本和审核检查。所有选项都可放弃，此时已选计划指针不变。计划采用只更新规划记录。

## 推演的服务入口

在单独的私有 NF stdio 配置中，把绑定设置为以下结构。原有凭据、数据库连接和对象存储字段沿用本机的 NF 配置。控制端验证已有 control_operator 身份和目标分支 ADMIN 权限；创作模型和 resume_operator 都没有此端口。

```json
{"mode":"creative_controller","work_id":"<作品 UUID>","branch_id":"<分支 UUID>"}
```

`nf_harness_simulate` 接受候选引用、规划任务及代际、角色任务及代际、种子、分支和步数上限、幂等键。使用 NF 已编译的各人物 BELIEF 和固定的 WorldRef 与故事时间，按条件评估目标或退路，保存假设工件与停止原因。`nf_harness_plan_decide` 将某一候选的采用请求交给原生计划服务，或在 option_id 为空时放弃全部。需要滚动审核的计划应传入精确审核引用。服务重检受权范围和版本，收据由 NF 持久化。模拟不会提交任何世界事件。

当前演算方法是确定性 RULE_REPLAY。预算引用来自 NF，服务分别报告本地演算消耗、模型 Token、执行次数与金额。未经调度的模型调用数为零。结果仅描述该起点和所给人物材料下的条件后果，文学质量仍为 NOT_EVALUATED。

[冻结接口](../../contracts/CREATIVE_HARNESS.md) · [正式实现](../../src/novel_factory/application/creative_planning.py) · [推演](../../src/novel_factory/application/creative_simulation.py)
