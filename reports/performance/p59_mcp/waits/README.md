# 模型在途、审核休眠与恢复

在独立 PG 17.11 上，锁定 SDK 1.29.0 经 stdio 协商 2025-11-25。两项新反例使用 0–2 号 CPU，通过测试凭证运行。额外重跑现有作者待批用例。原始文件每项记录业务响应、测量时间和预算，未写凭证与正文。

## 模型执行期间的短工具

真实 Supervisor 与模型网关向本地模拟 Provider 发送一次 HTTP 请求，在确认收到请求后将其阻塞 3.002 秒。在途 Attempt 为 SENT，任务 RUNNING，调度槽为 1，预留 0.100000，结算金额为 0。观察者 checkout 前同一网关连接池已借出连接为 0。此时经真实项目级 MCP 读取状态 6/6 正确；生产 TaskService 续租用时 10.71 ms，租约与 dispatch slot 到期时间在数据库中一致。两类操作都在放行模型前完成。Supervisor 的同步执行线程仍在等模型，区分线程、持久执行槽与数据库连接很重要。[逐项记录](provider.json)

| 同一客户端、同一工具 | 完成 | 最小 / 中位 / 最大延迟，ms |
|---|---:|---:|
| 模型发起前 | 6/6 | 26.11 / 31.34 / 35.35 |
| 模拟模型被阻塞时 | 6/6 | 25.06 / 39.76 / 67.68 |
| 模型返回后 | 6/6 | 25.86 / 28.48 / 37.82 |

这是确定先后关系的短样本；每阶段 6 次、一个活动模型，不能从中估计高并发尾延迟。模型返回有效内容与 token 数但未提供价格，正文已存、执行槽释放，Attempt 进入 UNKNOWN，保持 0.100000 预留。测试脚本提交独立费用回执 0.010000，预留归零，仍仅一条 Attempt。实际商业调用为零。[执行结果](test.xml)

## 人审边界

- **独立操作员的语义冲突决定。** 自动处理窗口上限设为一个节点，制造需要独立复核的案例。真实审核票据进入 WAITING_REVIEW，下游任务前置依赖未满足，状态 BLOCKED，没有执行租约、dispatch slot 或模型 Attempt。项目级 MCP 显示一项待审，首次 resume 唤醒 0。发起者自审被拒。脚本使用独立、受授予 APPROVE 与 ADMIN 权限的操作员调用现有 ConflictResolutionService 并登记证据，基于当前 HEAD 创建修复分支，重复提交返回同一回执。审核票据变为 ACCEPTED，重连后的项目 MCP resume 唤醒一项为 READY，再次 resume 唤醒 0。主世界 HEAD 保持案例建立后的版本。没有真人参与。该操作员决定属于可信应用服务，项目 MCP 会话只拥有查询、恢复权限。[完整记录](review.json) · [审核服务](../../../../src/novel_factory/application/conflict_resolution.py)
- **作者对未决编辑选择的审批。** 重跑已有 `UNRESOLVED` 全流程，以实际三次模拟 Provider 调用产生未决编辑意见，校验 WAITING_REVIEW / AUTHOR_DECISION_REQUIRED、pending_author_approval_count=1、规划选择及 HEAD 保持、后续 Supervisor 空转且没有第 4 次调用。前一项独立冲突票据的 pending_author_approval_count 为 0。此处只验证专属作者待批任务正确挂起，未验证真人通过 UI 或专用 MCP 按钮批准后自动恢复该稿件。[作者待批测试](../../../../tests/integration/test_p5_editorial_handoff.py) · [运行结果](author-park.xml)

初轮测试夹具假定返回创作结果便可结算费用，并误以为默认窗口不能自动产生修订方案。这两条断言分别被真实 UNKNOWN 和 REPLAN_PROPOSED 否决。第二轮给 UNKNOWN 提供独立回执，按生产配置将窗口设为 1 来触发人工复核，不改业务实现、权限或计费规则。[初轮结果](first.xml.gz) · [预登记](PROTOCOL.md)

## 复验

在配置仓库锁定依赖的环境中使用独立 PostgreSQL 17；`scripts/with_postgres.py` 提供运行所需数据库地址。以下命令在仓库根目录执行。

```bash
out=$(mktemp -d)
NF_MCP_WAITS_EVIDENCE="$out" python scripts/with_postgres.py --log-dir "$out/pg" -- \
  python -m pytest -q tests/integration/test_p59_mcp_waits.py \
  'tests/integration/test_p5_editorial_handoff.py::test_model_candidate_drives_change_window_and_editor[UNRESOLVED]' \
  --junitxml="$out/tests.xml"
```
