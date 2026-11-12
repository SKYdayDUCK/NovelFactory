# 架构、模块职责与接口审核

## 审核对象与方法

独立于实现的审查上下文在 main 推送验收成功后，从 GitHub 读取源码、调用方、迁移和测试。范围是旧主线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b` 至合并 `1de471076b8fb4240f95ff8033c60db34c187404`，比较记录为 21 个提交、183 个文件。产品 `602ba7fe8ece18503ac3b994adee4f00634cd761` 与合并使用同一树 `3fe25fcb17d2fd76618a0695b1d54641d71ce60f`。以下行号均对应这棵树。此次审核独立执行了导入关系分析和静态边界断言，读取了实际主线运行 43197896764 的 job 131113456379、131113456930 日志。在此上下文中未重新运行 PostgreSQL。源码判定、历史测试回执与生产部署实测分别记录。

## 权威状态与写入边界

| 路径 | 事实上的权威写入与回读 | 判定 |
|---|---|---|
| Core 世界状态 | `src/novel_factory/application/kernel.py:794-925` 的 `accept/_accept_transaction` 仍持有正式接受事务、令牌检查和修订选择。Harness 模拟在 `creative_simulation.py:298-350` 冻结候选、运行规则、重新取参与者切面，只登记报告、运行 artifact、幂等回执。静态扫描模拟模块找不到调用 Core accept 或写事件/修订的语句。 | S2 模拟结果可供决策，不会自行推进 world HEAD。 |
| 原生选定计划 | `creative_planning.py:217-258` 先验证计划绑定，`PlanningService.save_plan` 保存每条原生备选。`decide:294-316` 引用原生 plan_ref 并以同一事务调用 `PlanningService.adopt_plan`。原有 `planning.py:359-404` 检查预期父计划、分支 HEAD、APPROVE、滚动审核及 CAS。 | Harness 决策记录引用原生选定结果。原生计划采信之前仍存在动态权限缺口，见 SEC-02。 |
| 正文与审核 | `creative_review.py:175-181,294-317` 调用既有 `ReviewService.read_text/submit_review`，使用核验过的精确 text_ref 与跨度。`decide:459-489` 对正式保留/接受调用 `ReviewService.record_decision`。写手简报在 `creative_revision_brief.py:59-76,93-114` 重新取得绑定与被授权片段。 | 无第二份正式正文或编辑决定状态机。S3 的决定者身份与已撤权审稿 Skill 另见权限报告。 |
| Workshop 员工与 Skill | `workshop/src/agent_workshop/reviews_skills.py:104-124,132-153,169-199` 持有共享、撤权和快照。`dynamic_skills.py:40-60,64-115` 将组织、雇用、任务、安装、角色及原生回执相联。NF 的 `workshop_skills.py:44-58,120-169` 通过鉴权 stdio 取得期效见证。 | 没有 NF 直接查询 Workshop SQL 的应用导入。NF 存储可重放的内容，不能自行声明员工仍有权使用。 |
| NF 动态指令和优化 | `dynamic_agents.py:478-541,670-752,754-835` 区分实例选择、冻结和复代的输入。`adaptive_runtime.py:40-101,105-126` 按确切 ref 重建有界链。`adaptive_promotion.py:751-814,932 以下` 处理独立意见、预期 epoch 与回滚。 | 当前指令选择沿用 nf_selections，冻结内容沿用 nf_artifacts。它们确实是指令执行的权威状态，范围在角色/Skill；不是新的世界接受、员工授权或费用账本。 |
| 账务与观察 | `agent_gateway.py:549-555,607-642` 复用 TaskService reserve/send/settle；`candidate_delivery.py:40-72` 在持锁短事务验证、记录失败与 outbox。迁移 `0019_p57_context_sources.py:18-27` 新建索引，`0020_p57_harness_observations.py:14-17` 在原审计表追加可空字段和派生索引。Workshop `w011_harness_observations.py:13-35` 将 Skill 冻结写至已有投影。 | 未发现独立余额表或新世界事件日志。两库恢复仍需成对回执与已有隔离机制，观察字段不能覆盖原账。 |

## 入口、冻结与模块依赖

`AgentGateway.prepare_input:94-138` 检查原生 ADMIN、版本化角色、模型配置和开关，将结果写入正式 AgentInput。`_binding:142-215` 至少核对 task/generation、WorldRef、actor、角色、授权 epoch、知识 cut、输入 ref/哈希与冻结 runtime。`_messages:338-365` 实际发送冻结或刚投影的 user 字节。`creative_context_store.py:164-197` 按 context_bundle_id 定位并核对来源与代际。交付侧 `agent_gateway.py:636-642`、`supervisor.py:347-376`、`candidate_delivery.py:49-69,95-107` 把发送后和采信时的检查分开。S2、S3 直接服务不走这个 supervisor，不能借其它路线的合格回执推断自身有同样保护。

`mcp_stdio.py:198-212,223-261` 按私有配置构造项目、任务或控制器 binding；服务检查工具名单和 JSON Schema 后派发。`mcp_runtime.py:41-75,103-137` 按持久任务配置、租约、动态能力和角色列工具。`src/novel_factory/infrastructure/creative_config.py:11-36`，缺省三个功能开关和自动晋级均关闭。已有 TaskBinding 和 ProjectBinding 通过原分支解析。端点级 HTTP 全回归未在本审查上下文重跑。

我对两个服务 application/interfaces/infrastructure/orchestration 与 Workshop 包中取得的 113 个非空 Python 模块建立直接 `from ... import` 关系图。518 条包内边，模块顶层边没有多节点强连通分量，无 NF 与 Workshop Python 跨包直接导入。计入函数内延迟导入后有 8 与 46 个模块的两个强连通分量。具体的循环依赖是 `kernel._check_artifact_read:286` 引用 dynamic_runtime，而后者在第 5 行导入 Kernel 层 require。类似地，Kernel 注册 artifact 时在第 132 行依赖观测模块。延迟引入避免启动即循环，但跨模块权限修复很容易遗漏调用方，SEC-01/02 就发生在这些分支。这是模块依赖证据，不是 Python 循环崩溃。图不包含动态字符串加载、第三方依赖及全部包外工具。

## 运行证据与限制

- 主线工程任务的 harness 组实际为 68 passed，dual 组为 11 passed。范围从 `validation/run_p57_acceptance.py:19-48` 核对。未将每一项静态审查冒称为单独复跑的用例。
- `tests/integration/test_harness_context.py:34,62,94` 分别覆盖开启后重启、旧静态任务遇到新网关、恢复使用冻结字节。`test_harness_planning_simulation.py:148,201,214` 覆盖并发采信、HEAD 推进后拒绝旧计划和两个角色条件推演。`test_harness_review_workflow.py:104,153,233` 覆盖定向修订、保留与跨稿件证明。已检查这些用例的断言和实现入口。
- `workshop/integration/test_dynamic_late_delivery.py:12-90` 证明 Gateway → Supervisor 的晚撤权阻止任务接受。`test_harness_planning_mcp.py:17,66,81` 覆盖分离的 stdio 角色和控制器、异支 ADMIN；这些测试分别验证不同的入口。
- 源码层面确认本次引入的新工程边界缺陷 SEC-01、SEC-02、SEC-03，详见 SECURITY_REVIEW.md。涉及动态工作流的多租户部署应等待权限回归复核。没有发现可确认的 P0、世界接受旁路或重复计费账本。商业模型实作和文学质量未在此次复核中验证。
