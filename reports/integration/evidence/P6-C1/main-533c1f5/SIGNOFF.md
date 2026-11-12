# P6 全局 Token 预算主线签收

**MERGED_AND_VERIFIED**。PR [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) 于 2026-11-12 03:19:32 UTC 合并。实际主线 `533c1f526ef8841610d5c414e77ecbea8bbd5e35`，双亲为 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc` 和 `f96d26bcf449d8e7905099402ddb4ddcf8679a60`。候选、PR 合成提交与正式 merge 共享树 `86c524a7ba91e38fa3832a515e890a87ce7599cc`。

| 维度 | 主线实测 |
| --- | --- |
| NF 工程 | NF 单元与契约 398 项通过。PostgreSQL 17 全量 827 项通过，17 项因 runner 隔离限制跳过。预算与升级新增 9 项全部通过，迁移到 0018。 |
| 权限与预算 | 两个作品的管理员尝试跨作品读取、修改被拒绝，数据库阻止旧客户端超额预留。任务与账户金额、Token 四个上限以及并发预留均由原 NF 账本强制执行。 |
| 取消和故障恢复 | 取消与发送在任务锁上分别先到达。只释放可以证实尚未外发的当前代预留，旧代与 SENT/UNKNOWN 保留。旧版本的取消回执重放不增加命令、不二次退还。真正的跨版本二进制 + 命令行复验、PG17/Redis 专项共 35 项通过、0 跳过。 |
| 双服务与 WAL | Workshop 三个云端作业通过。独立主线实际 stdio 两项通过、NF 取消四项通过。100000 Token 处于 UNKNOWN 时恢复，两端同一 attempt 仍有全额预留；同一合成结算回执重放，只消费 18，剩 99982。P4 独立物理恢复通过。 |
| 容量 | 原 AT-30 仍为 **NOT_MET**。50 人两轮首发 251.29/241.05 RPS；200 人两轮 45.39/46.29；独立 200 人 51.12。1000 开环读的 5000 请求有 2166 个 200、2822 个 429、12 个传输错误，实测 wire 47.85 RPS，世界状态保持一致。六个比较/容量作业通过，四个原压力档未达标。 |
| 真实创作 | `P6_REAL_CREATION = BLOCKED_LOCAL_AUTHORIZATION`。文学质量 `NOT_EVALUATED`，外部模型请求为零。 |

[原始 PR 结果](../pr17-qualification.json)和[本地主线账本验证](nf-local-summary.json)列出版本、命令、通过与跳过。双服务各场景的 WAL 和账本见[主线独立复验](main-paired-summary.json)。[GitHub 主线逐项结果](qualification.json)和[主线原件包](hosted-evidence.zip)记录工程、正文样例边界及失败档位；本地 [NF 账本原件](nf-local-evidence.zip)和[两服务原件](main-paired-evidence.zip)可分别复核。

主线工作流分别为 [NF](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147438010)、[Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147437875)、[P4 WAL](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147437883)、[P2](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147437908)、[P3 扩展档](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147437927) 和 [P3/P4](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43147437939)。原门槛的后续工作在 [Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)。

下一阶段为现有 [#16 本地运行预检和部署](https://github.com/SKYdayDUCK/NovelFactory/pull/16)。
