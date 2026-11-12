# S1 创作上下文与派发

写作、演员、审核等岗位仍由 NF 签发 ContextBundle。开启 S1 后，网关在原任务和代际内核对 WorldRef、KnowledgeCut、授权主体及 epoch、冻结的角色/Skill 原文及摘要。编译器将该切面的事实或角色信念、获准的风格与人物声音、相邻正文、接缝和不确定性投影到模型用户消息的 creative_materials 字段。输出格式和任务身份由系统消息提供；引文引用独立传递。缺少风格或邻文时记录省略原因。只有经原阅读服务核对的材料才进入相应的创作材料集合。

## 启用和恢复

服务端环境变量 `NF_CREATIVE_HARNESS_CONFIG` 接受完整配置。下面是启用固定指令 Harness 的配置。配置缺失时新任务使用静态网关。任务准备时选择配置，已冻结的任务恢复后沿用该次选择。S4、S5 的功能验收按各自里程碑跟踪。

```json
{"schema_version":"1.0","kind":"CONFIGURATION","creative_harness":{"enabled":true,"dynamic_agents":{"enabled":false},"adaptive_instructions":{"enabled":false,"auto_promote":false}}}
```

S1 使用 NF 原有不可变工件存储，以编译上下文 ID 为键冻结控制包、实际模型输入、来源版本与 AgentInput 引用。派发前、保存模型结果、下游使用候选时复核任务与读取授权。原稿已有后继修订、选用的场景发生变化、授权被撤回或代际过期都可阻止交付。已发生的调用保留在 NF 原账本。静态任务没有新增快照、模型调用或自动优化操作。首次部署迁移 `0019_p57_context_sources` 为 NF 历史稿件的父版本新增部分索引。大表上执行建索引时会取得 SHARE 锁，运维窗口应包含该步骤。

## 复验

核心回归命令如下。在仓库已有的 PostgreSQL 17 测试环境执行。

```sh
python -m novel_factory.contracts.generate --check
python validation/validate_creative_harness.py
python -m pytest tests/unit/test_harness_configuration.py tests/integration/test_harness_*.py tests/integration/test_agent_gateway.py tests/queries/test_context_dependency_atomicity.py -q
```

阶段证据列出真实 PostgreSQL、确定性适配器及版本迁移的结果。真实付费模型调用为 0，独立文学评估保持 NOT_EVALUATED。S2 将以冻结计划、任务切点、角色认知、NF 额度及既有正式采用服务为输入。

[阶段证据](evidence/s1/receipt.json) · [使用的契约](../../contracts/harness.schema.json) · [冻结的 S0 候选](../../contracts/creative_harness/creative_harness.schema.json)
