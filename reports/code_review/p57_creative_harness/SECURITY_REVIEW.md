# 权限、身份、数据和指令边界审核

## 审核基线

独立上下文，旧主线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`，产品 `602ba7fe8ece18503ac3b994adee4f00634cd761`，合并 `1de471076b8fb4240f95ff8033c60db34c187404`。两后者共享树 `3fe25fcb17d2fd76618a0695b1d54641d71ce60f`。核对 §15.2、§15.4、§15.5 和 §16。以下结论来自定版源码与生产调用链。另查主线运行 43197896764 的 harness、dual job 终态与日志；68/11 个测试分别通过。审查未接触真实私有内容、身份文件、密钥或生产环境，未运行模拟入侵。普通权限反例给出静态证明和应补的正常业务回归，未声称动态复现。

## 已确认的问题

| ID / 级别 | 代码位置与前提 | 实际影响、建议与状态 |
|---|---|---|
| P57-SEC-01 / P1 | `src/novel_factory/application/agent_gateway.py:374-393,456-465,553,575-597`；`dynamic_runtime.py:112-130`；`kernel.py:273-307,349-360`。S1 开启、任务没有 dynamic/adaptive 选择；同一作品的兄弟分支控制器持 READ+ADMIN，且已获得目标审计对象的完整 ArtifactRef。 | 普通 S1 prompt 包含消息与冻结上下文引用，但权限辅助函数只识别两种可选 assignment ref。读路径只校验作品后即走 generic ADMIN 返回，随后读出对象。S1 产生的正文片段、角色材料或模型输出可因此被兄弟分支授权者读取。已有 generic ADMIN 快捷路径见旧主线 kernel.py:282；本次将分支受限的 S1 数据附着到通用 AGENT_* 上，却只对 S4/S5 增加了分支围栏。未发现标准角色 MCP 的原始对象读取工具，结论限于已知 ref 的 Kernel 读取边界。修复应从不变的 task/snapshot/attempt 解析完整 branch scope，覆盖旧产物及有/无选择器的审计种类；候选保持原 schema，借助关联表或已有 scope marker。状态 OPEN。 |
| P57-SEC-02 / P1 | `src/novel_factory/application/mcp_runtime.py:158-197,262-278`；`creative_planning.py:17-48,275-311`；`creative_review.py:37-57,383-412,423-448,463-477`。源角色合法提交计划候选/独立报告后，Workshop 撤销其动态 Skill，原 NF grant、雇佣和世界输入仍有效。后续调用者是有效的另一控制器。 | 决策事务只从原生 NF grant、静态 runtime、当前计划/审稿证据判定源角色，并未核验那份任务的 W4 Skill 或退出状态。规划仍可调用原生 adopt；编辑可以接受/保留或退回该报告。W4 独立性 `workshop/src/agent_workshop/review_independence.py:62-85` 检查源委托与 subject group，但不读取技能授权表；`reviews_skills.revoke:132-153` 只作 W4 撤销，NF grant 可一直有效。不会直接改写世界事件。修复建议复用 `dynamic_runtime.refresh_task/verify_task`，事务外取得证明、事务内按源任务重新核验被冻结工具和有效能力。S5 已在 `adaptive_promotion.py:251-295` 实现相同结构。具体覆盖 S2 propose/decide、S3 当前审稿人/写手读写及其结果的最终采信。状态 OPEN。 |
| P57-SEC-03 / P1 | `src/novel_factory/application/review_independence.py:53-71,160-191`、`creative_review.py:432-443,463-477`、原生 `editorial.py:501-523`。作者与最终控制器使用不同授权账号，但属于同一个实质主体；一名真实独立的第三方已完成审稿。 | S3 身份证明覆盖作品贡献者与审稿人，最终编辑未纳入其签名绑定。最终采信只比较 principal_id；Kernel 发放的另一份 grant 会生成新的 principal_id（kernel.py:423-425）。因此流程能证实独立审稿，但同一实质主体仍可以用另一个有效审批授权最终保留自己的内容。未断言作者可伪造审稿、绕过独立审核或无需编辑权限。对正式编辑决定绑定审批主体与其 grant/epoch 及源主体组；可借用 S5 的 publisher case，并在执行决定时复核。状态 OPEN；若产品允许作者本人以另一身份最终定稿，应明确写出该例外并修订独立性承诺。 |

## 独立核对的业务场景

1. **审计读取。** 阅读同树 `workshop/integration/test_dynamic_late_delivery.py:53-71`，其中有“同作品、兄弟分支、READ/ADMIN、同一 ref”的正规授权 fixture，断言启用 S4 后拒绝四类对象。追踪同一授权 fixture 在仅 S1 下的记录结构。独立执行源码断言核实 S1 的 prompt 持有消息与 creative_context_ref，权限辅助函数不消费它，以及 generic READ/ADMIN 返回路径存在。这一授权判定缺口可由两处条件直接证明。待回归同时覆盖本支可读、异支拒绝、原始无 marker 历史和有 marker 新数据。
2. **撤权与采信。** `test_harness_planning_simulation.py:273,358` 测试的是 NF planner/actor grant 被撤。`workshop/integration/test_dynamic_late_delivery.py:12-90,96 起` 真实撤销 W4 Skill，但经 Gateway → Supervisor。独立扫描 S2 规划和 S3 当前报告的完整调用链：两者不消费动态见证，S3 的 W4 subject attestation 不引用 Skill 表。建议以有效产物为起点，只撤 W4 绑定，保持 NF grant、原生 plan anchor/independence 有效，验证分别拒绝采信并保持原指针。另测未来相同正常调用、回放、撤权前后结果边界。
3. **实质主体。** `tests/integration/test_review_independence.py:17-40,44-77` 的签名资料恰好覆盖所有 producer 与 reviewer；原生 S3 身份组相同即拒绝审稿。检查 `_binding` 的返回字段不含最终决定者。`test_harness_review_workflow.py:153-179` 中正常保留路径用的是另一个 NF principal，未提供此编辑的组证明。S5 相对完整的对照为 `adaptive_promotion.py:185-217` 的 publisher binding 和 `validate_native_case:68-72` 对 publisher 的独立判定。待补两个独立 NF grant 同属一个真实主体的编辑回归，不使用真实身份资料。

## 已查到的正向与否定控制

| 议题 | 源码证据与核查结论 |
|---|---|
| 作品、分支、actor、知识时点 | `kernel._authorize_with_work:72-83` 对作品、主体、操作和显式分支授权。`queries.py:71-92` 绑定任务代际、lease、deadline 与 policy。Gateway 的 `_binding:142-172` 对齐 WorldRef、actor_id、grant epoch 与 BELIEF. `creative_context.py:143-146,171-187,205-229` 禁止人物获得编辑材料、其他平面事实或未授权 source。生产测试 `test_harness_isolation.py:31,71,102` 对照两个角色、异作品/分支正文和失效稿件。 |
| 组织与 Skill sharing | W4 `reviews_skills.py:104-124` 在同组织检查 owner ACTIVE、scope、mode、有效期和主体；ROLE/TEAM 对照现任角色/团队；PRIVATE 绑定 employee_id 与 employment_revision。`dynamic_skills.py:40-59,64-115` 将 W4 安装、源委托接收身份、scope、任务模板和 generation 对齐。NF `workshop_skills.py:101-169` 将这些结果限定至本分支 installation/slot 与当前 grant/epoch。 |
| Prompt Injection、角色私有材料 | `agent_gateway.py:317-340,357-366` 只把经冻结/核准的角色、Skill 和已发布块置入 system。未经信任的 assignment、正文/证据进入 user。动态 Skill 文字对 W4 源内容 SHA 校验，工具必须是冻结角色的子集（`dynamic_agents.py:236-243,263-276`）。系统/用户消息分离可防应用把正文提升成配置；无法证明某商业模型永不遵从正文指令。 |
| 撤权、生成后交付、旧快照 | `agent_gateway.py:226-239,635-642` 在标记发送及模型返回后刷新。主管 `supervisor.py:313-318,347-375` 在采信前重验；`candidate_delivery.py:36-69` 失败进 WAITING_REVIEW。动态已生成候选不会修改原始内容。普通 S1 快照通过 `creative_context_store.py:164-177,202-207` 复核 input、context、generation 及源指针。有效期不超过 10 秒的 W4 见证无法把独立数据库变成单一线性化事务；外部证明刚签发后的排队/失效窗口是单独的模型限制。 |
| 独立审核与指令发布 | 原生 `review_independence.py:31-72,106-128,160-191,247-264` 核验私有文件、HMAC、截止、正文修订链以及当前身份。W4 `review_independence.py:62-89` 绑定 review scope/团队、精确内容 SHA 及贡献组。S5 `adaptive_promotion.py:185-217,751-775` 将 evaluator 纳入生产者，区分 reviewer/publisher 并复核当前意见。线下 registrar 真实验证主体和签名密钥隔离是部署前提；单靠私有文件模式不能推知真实组织控制情况。 |
| 配置、输入契约、日志 | `mcp_stdio.py:189-212,234-239,262-297` 对私有配置文件做所有者/模式检查、调用前验证 JSON Schema、统一区分错误码，工具日志只取已识别对象引用。HTTP provider 在 `infrastructure/model_gateway.py:92-98,108-118,132-133` 拒绝含 userinfo/query 的 endpoint；API key 保存在私有属性，仅用于授权请求头。原始 response 会保存在审计 artifact（agent_gateway.py:574-577），不可把“指标不含正文”扩大为“所有持久化数据不含正文”。S6 观测模块 `harness_observations.py:72-105,187-233` 投影摘要/版本/计量。真实供应商自由字段与运维日志没有做现场取样。 |

## 总结

已确认三个 P1 代码缺口，未确认 P0。主线通过的测试支持角色 BELIEF、既有 Gateway 晚交付、本地签名和 W4 共享路径，不能用来宣称这些跨模块组合已通过。建议保持相应可选功能关闭或仅限单一受信主体的隔离环境，直到修复记录与前后对照回执齐备。历史物理部署、密钥实际保管、商业模型安全表现与文学质量未在本次核验。
