# P6 全局 Token 预算验收

PR [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) 将跨任务共享预算接入 NF 原有 attempt 账本。旧堆叠头为 `43dc369ab73e2faacb509b6ca16995848fc92c28`，整合基线为已签收的 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc`。冻结候选 `f96d26bcf449d8e7905099402ddb4ddcf8679a60` 与 PR 合成提交 `9529840b7b7138fc129bc795355a7f91bda6c781` 的 Git 树均为 `86c524a7ba91e38fa3832a515e890a87ce7599cc`。

| 边界 | 实际验证 |
| --- | --- |
| 作品权限和上下限 | 两个作品各自授权的管理员尝试跨作品查看和改限均收到 VIEW_DENIED。任务级和预算级金额、Token 限额逐一触发，超额插入事务回滚；100 Token 余额下两个任务并发争抢 60。 |
| 正常取消 | 旧版本的公开取消会遗留 3 Token 和 0.01 金额。相同回归在修前有两项失败。修复后，取消先取得任务锁时，当代 PREPARED 原子结算为零。发送先取得锁时仍保留全额。 |
| 历史兼容 | 使用真实旧二进制生成已消费 6、UNKNOWN 持有 10 的账本并升级到 0018。限额 20 时可用 4；旧二进制超额申请由数据库触发器拒绝。重复旧取消回执能找回漏退的两笔预留并保持原回执及 SENT。 |
| 物理恢复与组织账 | 100000 Token 全额 UNKNOWN 后恢复至指定 WAL LSN，两库保留同一 attempt 及状态。重复提交 18 Token 的合成结算回执只消费一次，NF 可用 99982。未创建 attempt 的组织委托取消按零结算。 |

真实创作仍为 `BLOCKED_LOCAL_AUTHORIZATION`，文学质量为 `NOT_EVALUATED`。原 AT-30 未达标的容量档位继续由 [#13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) 跟踪。

[本地验收清单](local-summary.json)和[本地原始回执](local-evidence.zip)包括修前失败、修后同测、旧版本二进制及真实 CLI、Redis 补跑、最终双服务 stdio 和物理恢复。ZIP 共 33 项，SHA-256 为 `9483a575b55f62e4f3944e0afc7d69fad1d9cfd34df158c83d58b396e8b9e6c0`。35 个不同的预算、任务和恢复测试均有通过回执。双服务测试单独运行，两项通过。

[PR 工程与容量逐项结果](pr17-qualification.json)和[GitHub 工件回执](pr17-evidence.zip)对应冻结候选与测试合成提交。六个工作流中，NF 完整性、Workshop、P4 物理恢复通过。NF 单元 398/398，PostgreSQL 全量 827 通过、17 跳过。其余三个工作流保留 10 个作业中 4 个历史容量失败。
