# P5-06 跨卷规划交接实测

第一卷经现有写作、独立审核及内部冻结流程产生 ReleaseRecord。ContinuityService 在独立审核后发布含一项未结伏笔的连续性快照。第二卷使用新 VolumeContract，明确引用第一卷快照。控制器将该快照列为第二卷 `volume_director/window_plan` 任务的固定材料，并将任务登记到 PostgreSQL 中的 Supervisor 队列。

启动新的 `scripts/run_supervisor.py --once` 进程后，它从数据库领取第二卷任务、编译固定 WORLD 视图，向回环 HTTPS 端点交付带来源的编辑性解释，保存一个符合 WindowPlan 契约的草稿。原始快照内的未来回收时点没有进入请求。给角色显式列上同一个输入引用仍得到 `VIEW_DENIED`。连续性读取继续校验任务代际、当前授权、故事时间、源正文、接受事件与 Retcon 状态。原有文学正文通道的岗位权限保持独立。

验收随后以已授权控制器调用现有 `PlanningService.save_window_plan`，核验卷合同版本并保存候选窗口；同键重放取得相同引用。Supervisor 的自动步骤截至产出受验证的候选。测试没有据此发布第二卷正文或变更已接受世界。再次启动独立 Supervisor 进程返回 IDLE，模型替身收到的请求总数仍为 1。逐一比对第一卷的发布前缀、完整快照、世界 HEAD 与事件数量，均保持原值。

实测 PostgreSQL 17.11。两次进程运行合计 1.3494 秒。测试端点报告输入 11、输出 7 个 token，为确定性夹具数据。连续性快照记录一项独立审核；本次调度不需要作者交互。完整输入输出引用、任务代际与计数量见 [机器记录](../evidence/p5/cross_volume.json)。

复现需要 Python 3.12、PostgreSQL 17 和 OpenSSL，命令在仓库根目录执行。

```bash
uv sync --locked --group dev
export PG_BIN=/usr/lib/postgresql/17/bin
export NF_P5_CROSS_VOLUME_EVIDENCE="$PWD/evidence/p5/cross_volume.json"
uv run python scripts/with_postgres.py -- python -m pytest -q \
  tests/integration/test_p5_cross_volume.py tests/integration/test_agent_gateway.py \
  tests/integration/test_continuity.py tests/integration/test_continuity_http.py
```
