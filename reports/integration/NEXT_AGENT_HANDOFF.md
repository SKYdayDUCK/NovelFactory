# 恢复入口

读取 [当前唯一阶段](CURRENT_STAGE.md)、[交付台账](DELIVERY_LEDGER.md)、[PR 矩阵](PR_MERGE_MATRIX.md) 和 [已知失败](KNOWN_FAILURES.md)。先从 GitHub 核实分支、SHA、合并时间及 CI，再读取原 PR 讨论。W2 正式主线 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1` 已签收，当前仅 NF Bridge #11 开放。后续不得并行开启。[W2 签收](W2_主线签收.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](evidence/NF-Bridge/main-15f606c/merge.json)
