# NF 受限委托桥接主线签收

**MERGED_AND_VERIFIED**。原 PR [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 于 2026-11-11T19:02:35Z 合入 `main`，实际提交 `15f606c99c765deeffd904ad540441eb6476b3af`。第一父为 W2 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`，第二父为冻结候选 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。正式候选和主线同树 `788dc4b190299894509593a23982452f0362f9ef`。

| 交付或门禁 | 实际结果 |
|---|---|
| 有限委托、租户和任期 | NF 专用 stdio 暴露 submit/get，每次按安装、作品、分支、策略和槽的角色授权核验。事务原子创建任务、Supervisor job、不可变回执和 outbox。相同操作重放同一任务；不同安装可各自使用同名操作/额度标识。 |
| 账本和出站权限 | 模型预留与派发两处复核授权，在真实 PG 锁等待后复核到期时间。被拒的调用人看不到操作的存在性。撤权、策略变化和恢复围栏限制新调用，原 UNKNOWN 按原 attempt 结算。 |
| 角色进度 | 已授权的本任务当前代际返回可见结果 UUID 或粗粒度 S9 等待原因；扫描前后均可辨认依赖冲突，其他委托、私有材料和过时代际的案件不可见。 |
| 迁移与实际服务 | 旧 W2 的 NF 0016 在 67 张历史表的非空数据上升至 0017；已使用的安装拒绝降级。两套独立 PostgreSQL 17.11、NF/AW stdio 及错置凭据均实测。候选和真实 main 各 18 项 Bridge NF、1 项双端委托、45 项 Workshop/W0–W2 回归，合计各 64 通过、0 跳过。[主线原 ZIP/JUnit](evidence/NF-Bridge/main-15f606c/receipts.json) |
| 工程完整性 | 候选 398 单元，PG 817 通过、17 托管权限跳过；本地干净候选相同 834 个测试逐例匹配、834 全过。[候选本地原件](evidence/NF-Bridge/candidate-695668b/local-full-regression.zip)。真实 main 398 项单元全部通过，完整托管 PG 817 项通过、17 项宿主限制跳过、1 项警告；两类固定历史对照、旧 Context Compiler 试验、六场景和 HTTP 行为验收全部通过。在支持 Linux 用户命名空间的本地真 main 另外跑 35 项，逐用例覆盖托管跳过的全部 17 项，均通过。[角色隔离及桥接本地 JUnit](evidence/NF-Bridge/main-15f606c/role-and-bridge.zip) |
| 物理 PITR | 常规 hosted P4 通过，三张桥接表为空。独立的候选和主线都用真实委托数据再做物理 WAL 回放。真 main 从 base backup 与 WAL 到 `0/312D9B8`，恢复70表、42对象；安装/槽/回执各1，原 UNKNOWN 及 0.100000 USD、100000 token 预留保留。目标后的结算排除，真 stdio 重放原回执，恢复代际 1→2；继续派发被拒。零目标提交丢失、零商业调用。[主线驱动与原日志](evidence/NF-Bridge/main-15f606c/nonempty-physical-pitr.zip) · [逐值 JSON](evidence/NF-Bridge/main-15f606c/nonempty-physical-pitr.json) |
| 500 Agent / 600 秒 / 20 RPS | 候选 12,000/12,000，P99 126.4198 ms。主线 600.134596 秒、12,000/12,000 有效 HTTP 200，19.99551 次有效请求/秒、P99 234.6994 ms，零正确性错误、零新世界提交、连接全部归还。1 小时 soak 仍为 NOT_EXECUTED。 |
| AT-30 既有失败 | #13 的两档热点、独立 200 与 1000 开环保留原红，所有触发的工作流均记录。主线 1000：5,000 请求，2,397 HTTP 200、2,600 HTTP 429、3 次传输错误，wire 53.03695/s，原目标 ≥950/s。[主线完整测量 ZIP](evidence/NF-Bridge/main-15f606c/capacity-originals.zip) · [候选完整测量 ZIP](evidence/NF-Bridge/candidate-695668b/capacity-originals.zip) · [发行依据 #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13) |

[所有主线作业及原始工件](evidence/NF-Bridge/main-15f606c/github-run-index.json) · [候选全部作业](evidence/NF-Bridge/candidate-695668b/github-run-index.json)。实际模型接口验收使用本地供应商 fixture。W6 专用剧情 fixture 仍可从原 `b1c830d` 取回，#17 的 P6 实验级共享预算未混入。

下一阶段仅开放 Workshop W3 [#10](https://github.com/SKYdayDUCK/NovelFactory/pull/10)。W4–W6 与 P6 冻结。
