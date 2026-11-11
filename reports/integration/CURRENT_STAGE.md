# 当前唯一阶段

W4 [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) 已于 2026-11-11 21:42:09 UTC 正常合入主线 `6c711adf794842bdf4469ab51e89093d37761bcc`。候选 `1cc12ca` 正式门禁通过。当前状态为 **MERGED_AWAITING_MAIN_VERIFICATION**，办理真实主线双服务、升级/故障、权限及两条 push CI 的复验；W5 仍冻结。[主线 Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43105371336) · [主线 P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43105371181) · [合并双亲、同树和两轮工作流](evidence/W4/main-6c711ad/independent/merge-checkpoint.json)。

## 先前的候选检查

当前 W4 [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) 的新候选是 `1cc12cab018213e05b70c226dcdf63974ee7dd93`。GitHub Actions 事故结案后，这次普通快进只重触发 CI，唯一父提交为 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`，相同的产品树为 `cb601579cceafd8cd33d35664a31f8d9dd38c879`。21:26:37 UTC 新 P1 已分配两项作业，Workshop 已建作业；旧两运行仍为零作业。新的精确 SHA 已独立跑完 13 套，123 项通过、1 个本地 Docker 检查跳过，另有 17 项原生角色测试及实际阅读覆盖、NF 报告和费用的附加复验。主线仍为 `e6763d844f023d7c6a13060dbaebca0416fe3099`，W4 必须取得正式 CI 和合并后复验才签收。[Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43102959511) · [P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43102959607) · [当前候选原件](evidence/W4/candidate-1cc12ca/independent/README.md) · [正式运行、合并模拟树和父提交](evidence/W4/candidate-1cc12ca/independent/candidate-checkpoint.json)。

## 先前候选的检查记录

Workshop W4 [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) → `main`。冻结候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`，树 `cb601579cceafd8cd33d35664a31f8d9dd38c879`。已签收基线 W3 `e6763d844f023d7c6a13060dbaebca0416fe3099`。此源继承 `6f07f8e` 的所有审核授权、增加真实 NF 的原文读取及类型化审核报告，并扩大旧库目录和权限验证。独立干净检出 [旧库](evidence/W4/candidate-3ed83ff/independent/preserved-original.xml)、[审核授权](evidence/W4/candidate-3ed83ff/independent/upgrade.xml)、[双服务报告与账单](evidence/W4/candidate-3ed83ff/independent/paid.xml) 合计 27 项通过，Linux 角色 [17 项](evidence/W4/candidate-3ed83ff/role-3ed83ff.zip) 通过。[Workshop CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) 与 [P1 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977) 截至 20:32 UTC 待分配执行。当前阶段仍须核对所有正式结果，正常合并，再独立复验主线并签收。W5 继续冻结。


2026-11-11 20:59:31 UTC。GitHub 的 [平台事故 dvy0t3mvh1lg](https://www.githubstatus.com/incidents/dvy0t3mvh1lg) 公告提及 Actions。此时原候选两项正式运行仍处于排队，尚无可重试的失败作业。独立干净工作树双库回归已经 [67/67 通过](evidence/W4/candidate-3ed83ff/local-w4-verification.zip)。继续跟踪原运行、执行合并及主线复验。W5 仍冻结。

截至 2026-11-11 21:02:59 UTC，公开候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72` 仍在已就绪的 #12 中等待托管执行，实际 `main` 保持 `e6763d844f023d7c6a13060dbaebca0416fe3099`。GitHub 在 20:48:25 UTC 将 Actions 组件标为 `major_outage`。两个正式运行均为 attempt 1、queued、0 个作业，待部署审批均为空。该候选的 NF 全量本地实测已达 834/834、0 跳过。恢复分配后核原运行与实际检出的 Git 树，两项托管运行完成后才能按正常流程合并、在实际新 main 复验并签收。W5 保持冻结。[GitHub 平台事件](https://www.githubstatus.com/incidents/dvy0t3mvh1lg) · [Workshop CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) · [P1 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977) · [NF 全量原件及三次运行](https://github.com/SKYdayDUCK/NovelFactory/blob/5f07125458a0249c3203c9c0d5b03ff92abdcd50/reports/integration/evidence/W4/candidate-3ed83ff/independent/local-nf-full.md) · [该时点的 GitHub 状态、PR、main、作业与部署审批回执](evidence/W4/candidate-3ed83ff/platform-outage-20261111T210259Z/checkpoint.json)。


**2026-11-11 21:15 UTC 续办点**。GitHub 再次确认 `main` 为 `e6763d844f023d7c6a13060dbaebca0416fe3099`，#12 未合并，候选 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`。正式 [P1](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096270977) 和 [Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43096271063) 均为 queued，作业数均为 0。已核已有“NovelFactory 串行交付续办”处于启用，按小时检查，上一轮于 21:10 UTC 结束，下次计划 22:04 UTC。恢复时先查询这两个运行和最新头；失败则诊断原日志，成功则核验原件、同树和审查，走正常 PR 合并、重验实际 main 并签收。只在 W4 签收后开启 W5。随后 W6 与双服务最终验收须针对真实 main 产出 `reports/integration/P5_P5.5_P5.6_主分支最终验收.md`，再恢复 P6。

2026-11-11 21:23:01 UTC。GitHub 于 21:07:26 UTC 公告正在处理积压，并于 21:21:39 UTC 将事故标为 resolved。此时 #12 的原 P1/Workshop 两项仍为 queued、0 jobs、0 待部署审批，head 和 main 均与上述检查点一致。继续以仓库原作业为合并门禁。[恢复监控](evidence/W4/candidate-3ed83ff/platform-recovery/211315-monitoring-checkpoint.json) · [官方结案后的实际排队状态](evidence/W4/candidate-3ed83ff/platform-recovery/212301-resolved-checkpoint.json)。
