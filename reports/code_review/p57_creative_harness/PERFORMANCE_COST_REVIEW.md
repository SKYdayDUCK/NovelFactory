# 性能、容量与成本审核

审计基线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`，开发交付 `602ba7fe8ece18503ac3b994adee4f00634cd761`，实际合并 SHA `1de471076b8fb4240f95ff8033c60db34c187404`。下文路径和行号指合并提交。材料包括实际执行入口、SQL、生产预算函数、原始回执、Actions 作业及 Issue #13。本次未在本地取得新的数据库执行计划、付费供应商账单或负载曲线。

## 影响资源的生产路径

| 阶段 | 有界条件与实际开销 | 审核反例或限制 |
|---|---|---|
| S1 角色包 | `creative_context.py:163–169,203–220,272–284` 去重相同来源，最多 64 个独立来源；以用户消息规范化 UTF-8 大小与任务预算比较。最终 `agent_gateway.py:465–470` 还检查完整消息与最大输出。固定快照按任务/代际保存。 | 预算单位是保守字节上界，实际供应商 Token 另计。内容产物、数据库 JSON 和对象存储仍增加长期占用；`source_fences:108–150` 对稿件、场景和样式逐一读。对大量小材料的 SQL 次数未做实测。 |
| S2 候选和推演 | `creative_planning.py:207–213,236–257` 单案至多 8 个节点，从合规节点派生一组原生候选。 `creative_simulation.py:172–248,279–287` 最多 16 个角色，入口 32 分支、每分支 128 步，同时还有候选实际选项、horizon≤32、Bytes/attempt 与单调时钟截止控制。确定性规则无供应商调用。 | 同候选的 advisory lock 持续到本地计算及写入结束；计算期间仍占一个连接和共享维护门。每一步重新编码角色 facts、条件并解析视图。正常分支竞争和撤权中止已实测，近上限 CPU/持锁分位尚未测。 |
| S3 文学审查 | `creative_review.py:77–107,448–472` 跨参与任务汇总现有 Attempt 的实际/未决预留，按原 quota、round 和独立再审约束修订。每个报告不会自动触发一次模型调用。 | 对每项 assignments 单独读取账本；这些局部输入字节、确定性规则和真实模型 Token 在数值上不可直接互换。 |
| S4 动态人员、技能 | 注册实例按 work、branch、identity 派生稳定 ID。相同角色新建修订，不必复制相应生产任务；已有任务使用冻结内容，送出前重查活体授权。默认动态关闭。 | 当前 history 一次取全链，两项版本列表的工作量随保留史增长。见 PERF-01。 |
| S5 优化 | `adaptive_instructions.py:845–854,893–927` 限定样本、重复与总文本工作量；`adaptive_evaluation.py:22–30,452–475` 限制 256 对，两个臂最多 512 个测量记录，个案 2 MB。固定 ID/事务锁复用完成的测量；预算/时间不足也记录失败理由。 | 持久测量数上界和外部付费次数是两个量。局部规则重放使用 0 供应商 Token。每例产生的 JSON、引用和写入同步仍有资源开销。 |
| S6 测量 | 指标端口在可重复读事务中限定作品、分支、鉴权控制员、半开时间窗≤31天、每来源 1–5000 行。 | 行限约束返回/聚合量，不代表索引读取、锁等待或一边倒数据的扫描成本也有同样上界。 |

[网关最终请求限额](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/agent_gateway.py#L465) · [本地模拟的有界循环](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_simulation.py#L172) · [逐测量可恢复执行](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/adaptive_evaluation.py#L534)

## 查询分母、历史时间窗与延迟

1. `creative_metrics._ARTIFACTS:81–143` 先按 work/time/kind/branch 固定候选集，再按工件主键取前驱。SQL 将正文、提示词、BELIEF 排除在输出中。S1 的 `packet_event` 按工件 evidence_id 关联；当前生产写事件时将同一修订作 event_id，主键去重，因而正常路径每个包一个事件。其余工件关联均使用全局 revision PK，避免金额/路径被多个来源重复扩展。历史动态库存另设 `limit+1` 下界，超限的精确库存为 null。[投影](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L81) · [生产 packet 记录](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/harness_observations.py#L187)
2. `_CALLS:157–171` 从 Attempt 出发，经唯一 task/budget、代际专属 selection、精确 binding/scene 修订关联。无绑定保留空场景，各币种、来源分别成组。窗口左闭右开，Attempt 以创建时间入样。截止时尚未结算或最终更新在截止以后，`_spend:190–212` 的金额保持未知；若窗内能看到已识别的模型回执，可保留实际 Token；存在任何未知项，对应总计为 null。真实测试两个场景各 65 Token，已结算 0.010000，另一场景金额空而 Token 65。见下文第一阶段角色查询反例。[窗口结算](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L157) · [主路径费用测试](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_creative_metrics.py#L191)
3. `_lifecycle:262–292` 从任务创建计到该代结果或终局；恢复率按不同的 task/generation 计分母，成功必须更晚且代际不更旧。两次重试不是两个新任务。编译/P95 取实际观察到的样本数及最近秩；窗口在结果到达前结束便没有该样本。网关 `execute:556,562,577,597,617–629` 的现有计时包括 generate、持久化 response/candidate 及账本结算。S6 读取这个值；要单独分析外部模型延迟，须另取仅覆盖 generate 的时钟跨度。夹具中的端到端延迟只适用于相同测量口径。[生命周期](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L262) · [计时起止](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/agent_gateway.py#L556)
4. `_rows:46–53,report:299–367` 使用 limit+1，超限拒绝返回截断比率。请求通常要运行 artifacts、calls、events、fences、inventory、compiled，另有存在演算时才运行 decisions。批查询数对窗口行数为常数；`_TASK_EVENTS` 的 text 格式 aggregate_id 与 task UUID 转文本、`_BRANCH` 的 JSON coalesce、库存从最早历史查到截止都可能使冷分支在大作品上扫描大量其他记录。0020 的 work/created_at/revision 索引没有 branch 表达式。默认授权从 `kernel.py:72–82` 获取作品和 grant 的 SHARE 锁，会保留到事务结束。未取得生产规模的 `EXPLAIN (ANALYZE, BUFFERS)` 或 pg_locks，所以不据此报告“实际全表扫描”或指定超时。PITR 的可重放 outbox 使用唯一键、SKIP LOCKED 和消费去重；没有取得大流量下 Redis 消息倍率和积压的实测值。[报告人口及限制](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L299) · [实际索引](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/migrations/versions/0020_p57_harness_observations.py#L14)

## PERF-01 · P2 · 动态实例历史查询无上限且逐条读、验、同步

**证据**　生产端口 `nf_harness_agent_history` 映射到 `DynamicAgentService.history`。实现 `dynamic_agents.py:628–668` 从当前 epoch 到 1 逐条追 event，再从 revision epoch 到 1 逐条追版本；请求无 limit/cursor。每一步调同文件 `_record:57–66`，通过 `Kernel.artifact` 执行一次 SQL，再用 `ObjectStore.verify:83–108` 读完整对象、计算 SHA256，并对文件及两个目录分别 fsync，另对数据库 JSON 计算摘要。外层事务从鉴权开始持有 work/grant SHARE。当前头部事件还会在 `_head:164–174` 及第一轮循环各读一次。

**触发和复核**　可用测试 `test_scoped_versions_rollback_frozen_bytes_and_archival` 的正常授权操作；已有 5 次生命周期事件、2 个版本，再查一次 history。按所检出循环边界和 verify 源码核算，仅两段循环即 7 次 SQL 工件读取与 21 次 fsync 调用，另有鉴权、读取头指针等固定工作。这里是源码语句计数，不是磁盘物理刷写次数或 SQL 跟踪实测。对 E 个事件和 V 个版本，下界分别为 E+V 次读、3(E+V) 次 fsync，响应也保留所有条目。

**影响、修复与状态**　仅已授权控制员能读取对应实例历史。长期运行的编辑项目可能拖慢历史浏览和同一作品的写锁申请，尚未测得生产延迟。建议以固定 head epoch 为游标锚，分页获取审核字段和受限数量的修订，保留作业式全量历史导出；验证跨页切换不重不漏及对象损坏检测，分别测 10/100/1000 个版本。此轮保留 P2，未修改运行时代码。[入口](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/mcp_runtime.py#L245) · [完整遍历](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/dynamic_agents.py#L628) · [文件与目录同步](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/infrastructure/objects.py#L83) · [现有真实实例历史](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_harness_dynamic_registry.py#L146)

## PERF-02 · P2 · 漏归类角色查询第一阶段调用，未知金额时也漏计已知 Token

**证据**　`actor_lookup.read_stage:92–146` 为角色选择查询正常注册一个 `actor-lookup-choice-v1` Attempt，保存 `AGENT_ROLE_TOOL_RESPONSE`，其顶层含 input_tokens、output_tokens、model_configuration。网关将这种工件记为独立的 `artifact.agent_role_tool_response` 事件，带 Attempt ID。S6 `creative_metrics.py:61–77,120–127,190–212,382–409` 仅收集 `AGENT_CALL_EVIDENCE`，后者属于最终候选阶段。第一步的钱款已经结算时仍从 ledger 进 known_amount，但来源变成 UNCLASSIFIED_LEDGER；每来源模型耗时和 Skill 次数也遗漏该步。第一步账单未知时，已收到的完整 Token 无法进入已知小计。

**复核**　原生夹具 `test_p5_actor_tool_choice.py:59–79,131–143,173–178` 定义供应商计数 22 输入 + 7 输出。unknown-first 只发一次并保留 UNKNOWN；read 正常发两次，既有 StatisticsService 支付指标明示 2 次、58 Token、USD 0.10。开发历史资格组 96 passed。此次取实际 `_KINDS` 列表和相同夹具数据，在独立 JS 中复算投影。unknown-first 的实际数据有 29 Token，S6 得到 known_tokens=0、unknown_token_attempts=1、actual_amount=null；正常两步在账本有 2 次，S6 的 Skill/实际来源仅 1 次，另外 1 次归未分类。该 S6 分组结果是源码驱动的独立计算，尚未在审核环境重新运行报表端口；历史测试本身验证的是既有 StatisticsService。

**影响、修复与状态**　受影响的是启用角色两阶段查询的工作负载，即使只有一次选择也会触发；角色账本、共享预留和阻断重发不受报表投影改变。建议按授权 work、branch、task、generation、attempt_id 从角色阶段的审计事件连接不可变响应，按响应实际结构提取已知 tokens/来源，明确标注缺少历史耗时的样本，防止与最终响应重复。应加入 unknown-first 与两步正常场景的 `nf_harness_metrics` 回归，保持 unknown 金额为 null。P2 未修；核对角色费用应以逐 Attempt 账本及已有阶段回执为准。[阶段回执](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/actor_lookup.py#L113) · [S6 数据来源](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L61) · [范围与分组](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/src/novel_factory/application/creative_metrics.py#L382) · [历史测试](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/tests/integration/test_p5_actor_tool_choice.py#L59) · [历史资格日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43196450625/job/131108555354)

## 真实容量与离线夹具开销

Issue #13 仍开放，AT-30 按原阈值为 NOT_MET。引用历史测量的版本和门槛，不将 S6 功能测试折算成吞吐。原始 `f2e6d38534214e79630b807cec9a720d99e975b1` 的 200 热点两轮 call-start 为 55.23 / 56.93 req/s（门槛 ≥950），header-start 35.11 / 36.22 req/s。1000 开环 5000 请求仅 1387 HTTP 200，header-start 43.32 req/s；500 持续读该轮 12000/12000。另一版本 `55d8979b5361a4837701d762feacd83036096b11` 的同一 500 原门槛仅 11508/12000、11954/12000，分别 492 和 46 个 429。后续同机 A–B–B–A 四轮 200 热点的 call-start 31.5210、34.4716、31.9154、33.7754 req/s，业务恢复四轮 PASS、原容量四轮 NOT_MET。该对照不隔离 P5.7 的因果贡献。仓库另存 50 闭环补测 293/300 与 300/300，均不能替换 AT-30。[Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) · [同机原始四轮](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43039855189) · [永久归档](https://github.com/SKYdayDUCK/NovelFactory/tree/373cb63305d2066013e2e4a6b42382d801bd189f/evidence/at30/s9-nf-delegation-abba) · [S6 补测范围](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/S6_ACCEPTANCE.md#L19)

| 归档开发校准：每臂 1 场景 | 端到端实测 | 本地计量单位 | NF Attempts | 已结算 USD |
|---|---:|---:|---:|---:|
| A 静态 | 2.734396505 s | 47,788 | 3 | 0.000000 |
| B 固定 Harness | 3.307728241 s | 40,685 | 3 | 0.000000 |
| C 动态与已批准指导 | 8.538710729 s | 41,347 | 3 | 0.000000 |

同一个本地环回模型的校准窗口，C 的时间包含评估和批准。四次本地优化回放另记。三个窗口均 CLEAR，真实商业请求 0，配对样本 1，独立确认批 0，文学质量 NOT_EVALUATED。以上逐臂时间来自提交中保存的原始三臂 JSON。另有实际 main 的 `comparison` 作业 1 passed in 29.44s，该作业总耗时不是任何一臂的耗时，也不是模型供给侧延迟。[逐臂原始测量](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/evidence/s6/three-arm-comparison.json#L1) · [计量口径](https://github.com/SKYdayDUCK/NovelFactory/blob/1de471076b8fb4240f95ff8033c60db34c187404/reports/harness/S6_ENGINEERING_COMPARISON.md#L20) · [main 作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764/job/131113456646)

**保留风险**　`kernel.py:126–133` 对每个永久工件登记数据库和对象引用，审计再透过事务 outbox 投递。合并增量没有发现针对仍被引用的稿件历史、冻结及试验测量的 TTL；有意保留的可追溯记录和未知账本不能直接按日期清除。没有生产历史基数、单位时间 DB/WAL 增长、备份体积和真实模型定价，因此没有给出月费、保留期限或线上延迟预测。
