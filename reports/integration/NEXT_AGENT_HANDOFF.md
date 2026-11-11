# 恢复入口

读取 [当前唯一阶段](CURRENT_STAGE.md)、[交付台账](DELIVERY_LEDGER.md)、[PR 矩阵](PR_MERGE_MATRIX.md) 和 [已知失败](KNOWN_FAILURES.md)。先从 GitHub 核实分支、SHA、合并时间及 CI，再读取原 PR 讨论。W2 正式主线 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1` 已签收，当前仅 NF Bridge #11 开放。后续不得并行开启。[W2 签收](W2_主线签收.md)

#11 已改投 W2 主线。候选 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 在原验收代码中补齐了安装到期时的预留和发送围栏。真实双服务、两处 WAL 落点和到期正反例已复验。核对本候选五项托管工作流及原始容量数据后再决定合并。[候选证据](evidence/NF-Bridge/candidate-26bf033/README.md)
