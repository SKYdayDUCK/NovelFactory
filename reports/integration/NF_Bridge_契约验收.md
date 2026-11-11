# NF 受限委托桥接

当前阶段为 [PR #11](https://github.com/SKYdayDUCK/NovelFactory/pull/11)，已签收 W2 主线为 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。原始桥接 `90cac947c6f1b176f20df6a579027f24e96d7b4c` 与主线的共同祖先为 `97eeceac266016b0d5da62786b019b704bfcdb70`。两父整合 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 保留原提交和先到期检查。候选冻结和实际主线结果按 SHA 保存于原 PR 和交付台账。

| 对应要求 | 生产路径 | 具体证据 |
|---|---|---|
| FR-XSYS-01～07，AT-46、AT-47 的 NF 前置，受限委托及身份隔离 | `application/delegation.py`、`interfaces/delegation_stdio.py`、`scripts/manage_workshop_binding.py` | 使用实际 MCP 进程和不同的 NF/AW 数据库身份校验两个 NF 工具、交叉绑定、错误作品和其他组织、同操作重放、在线撤销。已接受结果只返回本任务受权引用。 |
| 预算、角色与代际 | `orchestration/tasks.py`、`application/runtime_tasks.py`、`application/supervisor.py` | 委托的创建、Supervisor job、不可变回执及 outbox 同事务；两个不同安装可以使用同名操作及 hold；每个真实槽和费用尝试只消耗一次。模型预留与发送均复核当前安装、策略和角色授权。过期后既有 UNKNOWN 仍可按原 attempt 对账。使用本地 HTTP 供应商夹具。 |
| 恢复与数据库升级 | `0017_nf_workshop_delegation.py`、`infrastructure/recovery.py` | 用已发布 W2 创建含真实事件的 0016 数据库，核查原 67 表及业务表逐行摘要。未使用桥接时可退回 0016，有安装后拒绝降级并保留全库。已恢复的 NF 拒绝新派发，按任务、代际、逐笔 attempt 对齐后才解冻。 |
| S9 进度与结果 | `application/delegation.py`；[接口要求](../../docs/P5.6_MCP交互与权限隔离.md) | 以当前授权安装的任务和代际核查依赖、审核、待对账调用和已接受结果。回执不携带私有正文、对象散列、其他员工、冲突证明或角色认知。 |

## 原始失败与范围

- A 类为已排队的委托在安装到期、策略或槽权限变化后仍可能预留或派发；新增实际 PG 回归覆盖两处分派时刻、数据库等待越过到期边界及已 UNKNOWN 的原账单结算。独立底 `736baa3703368d3648bf950c42fe2b96a6e5497f` 的 13 项真 PG/stdio 全过。[原 JUnit](evidence/NF-Bridge/preflight/inference-authority.xml)
- B 类为旧 S9 升级测试固定当前 head 等于 0016。W1 预演发生 1 项失败；随后使用 W2 与原桥接集成再次复现，1 失败、5 通过。旧 S9 目标及降级保护单独保留，另验实际 W2 到桥接 head 的升级、数据与降级。[W1 失败](evidence/NF-Bridge/preflight/preflight-pg-130709065242-failure.log) · [W2 重现](evidence/NF-Bridge/preflight/w2-integration-before-fix.xml.gz) · [两类独立数据库验收](evidence/NF-Bridge/preflight/upgrade-and-stdio.json)
- C 类保留 [#13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) 原 50/200 热点、独立 200、1000 开环的门槛和失败。500 Agent 持续 600 秒的 20 RPS 项单独核对。早期 11,991/12,000 与相同 SHA 重试 12,000/12,000 结果保留；后续不能以这些历史结果代替正式候选或主线的完整负载。[8 次历史/预演工作流、18 份工件](evidence/NF-Bridge/preflight/historical-ci-index.json)
- `b1c830d` 的两个剧情 fixture 扩展只有后续 W6 使用，保留原提交，到 W6 时按依赖复核。#17 的共享实验预算属于 P6。独立 Scion 实验继续冻结。W3 的 Workshop Saga、组织端恢复及双方错位 PITR 仍由 W3 验收；真实商业模型创作尚未授权。
