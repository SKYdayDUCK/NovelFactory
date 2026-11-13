# P6 滚动生产的恢复与防重验收

| 中断或变化 | 实际观察 |
|---|---|
| 进程内 Supervisor 对象在调度调用后重建 | 新实例从 PostgreSQL 的当前生产板、`ROLLING_CURSOR` 和不可变章检查点继续。三章跨三个窗口按最初顺序冻结，每场写作、认知评阅和事实核查及每章读稿各执行一次。 |
| 写手在模型已经收到请求时被操作系统终止 | 运行在独立 Python 进程的写手在真实 PostgreSQL 中领取 3 秒实验租约，回环 HTTP Provider 确认请求已到达后由父进程 kill。重启的 Supervisor 等待租约自然过期，原代际的唯一 Attempt 由 SENT 变为 UNKNOWN，任务递增代际进入 PAUSED，租约释放，模型请求总数始终为 1，已冻结章节为 0。该测试于 7.32 秒通过，见 [原始回执](evidence/writer-process-exit.xml)。 |
| 尚未发送模型请求的写手被取消 | 以原申请者、run ID、窗口和 node ID 对该节点定向重排，保留旧 task ID/reason，同时解开仅因该前驱停止的子节点。重复提交同一幂等键返回相同回执。重排后的新 task ID 不同，至其再次派发前模型调用数为零。 |
| Provider 已收到请求但响应失败 | 故障后同一已送出的请求在账本中仍未明确结算。重排返回 `CALL_RECONCILIATION_REQUIRED`；无第二次调用，冻结章数为零。 |
| 输出已验证、账单金额仍未知 | 开发中的跨窗口实验在下一板注册处获得 `CALL_RECONCILIATION_REQUIRED`。原有预算门禁保留。正向实验使用显式声明零费的本地 HTTP 工程模型，生产样文仍应核对真实账单。 |
| 读稿与世界事实未完成 | 不能从“已存草稿”或文学意见推导事实批准。无依据分类留下 `RECONCILIATION_REPORT` 的阻断条目；缺少全文覆盖的任务保留失败。未产生章节接受。 |
| 已接受的抽取员任务在冻结时被重验 | 首次接入自动抽取时，旧的历史校验将已完成抽取员误判为不可用。修正仅允许在重验既有可信报告时使用已完成任务。原 generation、grant epoch、全文交付、语义依赖及当时世界证明仍逐项重验。 |
| 试图替换冻结章或跳过卷间审核 | 替换冻结章草稿被 `SERIAL_PREFIX_PROTECTED` 拒绝。首卷缺少已审核连续性快照时停在 `CONTINUITY_REVALIDATION_REQUIRED`，补全真实服务回执后自动开启次卷。 |
| 本地修订缺少当前输入 | 仅未打开且处于本作者许可中的窗口可按预期窗口引用局部修订。已经启动或冻结的窗口拒绝重排。 |

工程实验通过 `tests/integration/test_p6_rolling_production.py` 触发这些情况。在运行中的服务上，使用 `--rolling-status` 或 `--pipeline-results` 检查当前选区、节点与旧任务记录。对明确未发送且已停止的工作，使用 `--retry-rolling <private-retry.json>`；请求携带 `world_ref`、`run_id`、`window_index`、`node_id`、`idempotency_key`。对尚未打开的范围采用 `--replace-rolling-window <private-replan.json>`，并携带当前的 `expected_window_ref`、完整 `chapters`。已发送的未知 Attempt 继续使用既有财务对账边界。

本轮窗口用例覆盖 Supervisor 重建、实际写手进程终止及租约过期、单节点停止、费用不明、同卷和跨卷恢复。真实模型限流、长时间租约到期后的真实创作恢复及实际平台的长时运行，没有在三部正式作品上取得回执。已有 P5.9 进程/租约测试的结果不能补充这些作品回执。

### DeepSeek HTTP 原始回执

使用正常 NF DeepSeek RuntimeModelAdapter 发出的后续请求，会在私有对象根目录的 `http-receipts` 中保存每个 Attempt 的独立文件。独立 HTTP 子进程收到上游字节后，先在私有目录中写临时文件、刷盘并以不可覆盖的文件名发布，再把响应交回主进程解码。原件包括原始响应字节、状态码、请求哈希、供应商回执及收发时间；上限为 2,000,000 字节，超限保留有明确标记的前缀。原件不保存请求头或上游 API 凭据。存放目录含未公开作品及未经筛选的供应商响应，备份时应与原对象根目录一起备份并保持私有权限。

管理员可以使用对应的私有项目绑定执行下列本地查询。返回内容仅含元数据、原件哈希与实测用量，绝不触发供应商派发、接受正文或结算发票。旧 Attempt 如有仅存提示、缺失业务响应或未记录供应商发票，可由同一入口分辨。HTTP 子进程在收到回包前也被终止、机器断电或目标文件系统不可写时，可能没有可恢复原件；此类请求保留原有防重复派发和待对账边界。

```sh
uv run python scripts/p6_inspect_attempt.py --binding "$NF_P6_BINDING" --attempt "$NF_ATTEMPT"
```

回环测试覆盖父进程被杀、子进程仅收到一次请求后持久保存不合约响应、跨项目拒绝、私有原件哈希不符拒绝、原 Attempt 的金额继续待结算。[PostgreSQL 17 与传输回归](evidence/deepseek-response-recovery-pg.xml)
