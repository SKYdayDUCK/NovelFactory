# S4 动态角色、Skill 与任务版本

`NF_CREATIVE_HARNESS_CONFIG` 由 NF 服务进程读取。在静态部署中无需配置。在需要版本化岗位的部署中使用以下完整配置。

```json
{
  "schema_version": "1.0",
  "kind": "CONFIGURATION",
  "creative_harness": {
    "enabled": true,
    "dynamic_agents": {"enabled": true},
    "adaptive_instructions": {"enabled": false, "auto_promote": false}
  }
}
```

## 来源登记与版本选择

Workshop 的现有 W4 管理接口登记 Skill 的命名空间、任务模式、版本和内容摘要，并向员工、团队或角色共享。NF 在本地保存匹配摘要的 UTF-8 Skill 原文及受权岗位指令。Workshop 保存核验所需的标识、授权、摘要和任务快照。NF 的可信配置通过 `NF_DYNAMIC_WORKSHOP_BINDING` 指定员工的私有 Workshop 连接文件。多个安装或员工可用 `NF_DYNAMIC_WORKSHOP_BINDINGS` 指向私有 JSON 数组，以 installation_id 及 scope_ref 或 employee_id 映射到 binding_file。员工、租户、安装和授权代际由两端查验。

| NF 控制端操作 | 输入和作用 |
|---|---|
| `nf_harness_agent_register` | 提供已委托的来源 task_id、generation、W4 绑定、profile_version、岗位和 Skill 原文、请求的工具、触发原因、证据引用、NF 额度和幂等键。返回实例、版本和 S0 RoleSkillProposal 的引用。 |
| `nf_harness_agent_select` | 用 WorldRef、实例、版本与 expected_epoch 选择新任务使用的版本。首次选择使用 epoch 0。切回历史版本时提供该版本引用和当前 epoch。 |
| `nf_harness_agent_history` | 按受限的 WorldRef 和实例查询当前版本、epoch 和生命周期记录。 |
| `nf_harness_agent_retire` | 用当前 epoch 执行 EXIT 或 ARCHIVE，保留原始登记和冻结历史。 |

这些端口仅在动态开关开启的 `creative_controller` 私有绑定下可被发现。作用域校验采用控制端绑定的作品和分支。请求中的 expected_epoch 用于处理同一指针的并发修改。角色任务不能调用这些管理操作。

## 实际派发

在既有 `scripts/manage_workshop_binding.py` 的 slot 定义中，为需要该实例的 `definition` 增加 `dynamic_instance_ref`。其值是登记返回的完整 ArtifactRef。管理命令通过 RuntimeTaskService.define 为即将创建的任务冻结选择，再沿原有 NF/Workshop 协议安装 slot 并委托。未选用实例的任务走既有角色和 Skill 路径。旧 AgentInput 可以直接读取。

动态任务的 AgentInput 保存 `dynamic_assignment_ref`。它约束目标任务、代际及精确版本，并与已登记的静态角色基线一起进入审计。后续版本切换只影响后续分配。全局开关控制新实例及新任务的选择。已运行任务保留自己的配置与版本快照；要中止其动态交付，可撤回对应的 NF 权限或 Workshop Skill 授权。派发前需要当前 NF Grant、任务与 W4 员工/Skill/委托均可用。正文调用仍使用原生 Attempt、预算和模型路由。取得新岗位指令不能扩大角色的工具上限或改变人物的 BELIEF。

租约失效后，NF 用新一代任务的实际输入、世界切点、原选择及当前的 W4 委托重新核验指令；代际回执引用原冻结物。旧持有者不能交付。已发送但费用未知的 Attempt 继续占用原预算，新一代不会重复发送。

[角色登记和版本选择](../../src/novel_factory/application/dynamic_agents.py) · [NF 与 Workshop 核验](../../src/novel_factory/application/workshop_skills.py) · [W4 任务冻结](../../workshop/src/agent_workshop/dynamic_skills.py) · [阶段证据](evidence/s4/receipt.json)
