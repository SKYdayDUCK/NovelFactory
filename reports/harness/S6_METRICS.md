# S6 工程指标口径

授权控制端通过 `nf_harness_metrics` 或 `CreativeMetricsService(kernel).report(principal, world_ref, *, since=None, until=None, limit=1000)` 读取同一份报表。需要该作品和分支的有效 `control_operator` 身份及 `ADMIN` 能力。数据库使用一个可重复读事务。指标和时间边界同时返回，报表不回传正文、Prompt、审核引文、员工身份或角色 BELIEF。

`since`、`until` 是带时区的 ISO 时间戳。区间包含起点并排除终点，默认过去 24 小时，最长 31 天。`world_ref` 校验真实作品及分支，观察范围包括该分支在选定时间窗内的各代际。每类来源默认最多读取 1000 行，上限 5000 行。超限返回 `CAPACITY_LIMIT`，缩短时间窗口或提高限额可重试。注册实例存量是跨窗口指标，超过限额时单独给出下界。回看旧账期时，若账本状态最后一次更新晚于截止时间，金额保持未知。

| 请求项 | 返回字段与计算方法 |
|---|---|
| 上下文时间、大小和来源数 | `context.compile_ms/compiled_bytes` 来自成功 `context.compiled` 的耗时与编译对象的实际规范化 UTF-8 字节。`packet_build_ms/model_input_bytes` 来自被冻结的 S1 模型输入，`stored_snapshot_bytes` 取完整不可变快照对象的存储大小。`loaded_sources` 逐份计算 S1 最终列出的独立来源。历史快照没有耗时观察时，分布只统计实测项。 |
| 规划与推演 | `planning.candidates/alternatives` 为创建的候选记录与候选选项数。`simulated_paths` 只统计实际执行过步骤的选项。采用率和废弃率的共同分母是本窗口确已推演且已有正式终局决定的路径。采用原生选中项，其余已推演路径计为废弃；整案拒绝时所有已推演路径计为废弃。未决定项另计 `pending_paths`。 |
| 审核覆盖、发现、修复与返工 | `review.coverage` 以窗内新建稿件版本为分母，分子为截至窗末被有效 S3 记录实际覆盖的其中版本。跨章审核按覆盖稿件去重。`reports_by_layer` 按审核层级计数。`reports_with_actionable_findings` 是有具体修订请求的报告数除以有效报告数。`independently_fixed` 以原报告的可处理问题为分母，只将经过修订、独立再审、原生编辑 `ACCEPT_REVISION` 的具体原问题计为已修复。`targeted_revisions` 统计实际定点修稿，保留、推迟和退回本身均不算修复。 |
| 审核误报与漏报 | `heldout_review_errors.CONTROL/TREATMENT` 分别统计完成、独立标注、与训练稿去重的留出**工程审核运行**。每次重复运行是一个观测单位。按模式和精确跨度核对 TP、FP、FN。误发现比例为 FP/(TP+FP)，漏报比例为 FN/(TP+FN)。阴性案例误报率为产生任何 FP 的已标注阴性运行数除以全部已标注阴性运行数。无合格数据时误报、漏报数量与比例均为空。上下文排序、纯投递就绪测量、未执行行不计入。 |
| 动态实例与 Skill | `dynamic.instances_created` 是本窗口新增的不可变实例，`instances_registered_as_of_end` 是截至窗末的历史注册实例总数，包含后来退出者。切换和退出按 `actions` 单列。`dispatched_skill_versions` 只计实际模型调用审计中的不同 Attempt，列出静态 Skill 名称/版本以及已使用的动态修订与内容摘要。可能含私有文字的版本标签以摘要显示。注册或冻结本身不构成一次调用。 |
| 指令候选、评估、晋级与回滚 | `instructions` 按候选创建、完成工程报告、全部配对执行完整、符合工程门槛、正式批准、拒绝、回滚分别计数。未完成的工程报告计入 `evaluated`，并从 `complete` 中排除。实验重复调用不会制造新的报告或决定。 |
| 每场景 Token 和费用 | `scenes` 按同一代际的原生写作绑定归属到场景，再按预算币种和执行来源分别汇总。无确切绑定时 `scene_id=null`。已结算实际用量和金额出自 NF Attempt 账本；账务仍未知但已有完整输入和输出 Token 的调用可从模型审计计量 Token。`known_*` 是可核实的小计。有任何未核实项时对应的 `actual_*` 总计为空。不同货币不会合并。保留额度不会当作支出。 |
| 延迟和 P95 | 任务创建到该代际 `RESULT_STORED`、任务创建到终态（接受/失败/取消）、以及实际模型调用的时间分别计算。模型调用按执行来源分开。每个分布包含样本数、总和、均值及最近秩 P95，取升序第 ceil(0.95 × n) 项，单位为毫秒。空样本的数值为空。 |
| 错误恢复率 | `recovery.success_after_failure` 的分母为窗内进入 FAILED/PAUSED 或发生恢复、冲突重绑、备份恢复隔离的不同任务代际。只有同一任务在其后、同代或后继代际实际进入 RESULT_STORED/ACCEPTED 才计恢复。一次重新排队或重新领取仍留在分母中。结果为窗末状态。 |

所有比率同时给出分子和分母，分母为零时比率为 `null`。账本调用、模型审计、演算和实验运行保持各自的计数单位。工程误差率和夹具成本不构成商业模型质量或独立文学结论。

[查询实现](../../src/novel_factory/application/creative_metrics.py) · [PostgreSQL 集成回归](../../tests/integration/test_creative_metrics.py)
