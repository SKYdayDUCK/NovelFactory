# 当前唯一阶段

NF 受限委托桥接 [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) → `main`。W2 已签收于 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`，当前候选 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`，tree `788dc4b190299894509593a23982452f0362f9ef`。前一个候选 `b0fac90f71903b7349ccd57e643ae145148180d0` 的 61 项真实服务与升级专项通过，[归档原件](evidence/NF-Bridge/candidate-b0fac90/receipts.json)。后续仅追加了不可区分的受限调用拒绝和两处真实 PG 锁等待的回归。应复查实际 `git/ref/heads/dev/nf-workshop-delegation`，同步新候选的工程、迁移、物理 PITR、双服务与容量作业。

保留 50/200 与 1000 原容量限制，对 500/600s 单独核对。常规 PITR 的三张新桥接表为空；正在执行带真实安装、槽、回执与 UNKNOWN 金额的补充 NF 恢复。待本阶段经 PR 正式合并、在精确 main 上复验、保存回执，才开放 W3。其余阶段冻结。
