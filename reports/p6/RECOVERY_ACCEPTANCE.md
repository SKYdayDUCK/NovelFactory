# P6 滚动生产的恢复与防重验收

| 中断或变化 | 实际观察 |
|---|---|
| 进程内 Supervisor 对象在调度调用后重建 | 新实例从 PostgreSQL 的当前生产板、`ROLLING_CURSOR` 和不可变章检查点继续。三章跨三个窗口按最初顺序冻结，每场写作、事实核查和读稿各执行一次。 |
| 尚未发送模型请求的写手被取消 | 以原申请者、run ID、窗口和 node ID 对该节点定向重排，保留旧 task ID/reason，同时解开仅因该前驱停止的子节点。重复提交同一幂等键返回相同回执。重排后的新 task ID 不同，至其再次派发前模型调用数为零。 |
| Provider 已收到请求但响应失败 | 故障后同一已送出的请求在账本中仍未明确结算。重排返回 `CALL_RECONCILIATION_REQUIRED`；无第二次调用，冻结章数为零。 |
| 输出已验证、账单金额仍未知 | 开发中的跨窗口实验在下一板注册处获得 `CALL_RECONCILIATION_REQUIRED`。原有预算门禁保留。正向实验使用显式声明零费的本地 HTTP 工程模型，生产样文仍应核对真实账单。 |
| 读稿与世界事实未完成 | 不能从“已存草稿”或文学意见推导事实批准。无依据分类留下 `RECONCILIATION_REPORT` 的阻断条目；缺少全文覆盖的任务保留失败。未产生章节接受。 |
| 已接受的抽取员任务在冻结时被重验 | 首次接入自动抽取时，旧的历史校验将已完成抽取员误判为不可用。修正仅允许在重验既有可信报告时使用已完成任务。原 generation、grant epoch、全文交付、语义依赖及当时世界证明仍逐项重验。 |
| 试图替换冻结章或跳过卷间审核 | 替换冻结章草稿被 `SERIAL_PREFIX_PROTECTED` 拒绝。首卷缺少已审核连续性快照时停在 `CONTINUITY_REVALIDATION_REQUIRED`，补全真实服务回执后自动开启次卷。 |
| 本地修订缺少当前输入 | 仅未打开且处于本作者许可中的窗口可按预期窗口引用局部修订。已经启动或冻结的窗口拒绝重排。 |

工程实验通过 `tests/integration/test_p6_rolling_production.py` 触发这些情况。在运行中的服务上，使用 `--rolling-status` 或 `--pipeline-results` 检查当前选区、节点与旧任务记录。对明确未发送且已停止的工作，使用 `--retry-rolling <private-retry.json>`；请求携带 `world_ref`、`run_id`、`window_index`、`node_id`、`idempotency_key`。对尚未打开的范围采用 `--replace-rolling-window <private-replan.json>`，并携带当前的 `expected_window_ref`、完整 `chapters`。已发送的未知 Attempt 继续使用既有财务对账边界。

本轮窗口用例覆盖 Supervisor 重建、单节点停止、费用不明、同卷和跨卷恢复。正在写作时杀死 OS 进程、真实模型限流、长时间租约到期后的真实创作恢复及实际平台的长时运行，没有在三部正式作品上取得回执。已有 P5.9 进程/租约测试的结果不能补充这些作品回执。
