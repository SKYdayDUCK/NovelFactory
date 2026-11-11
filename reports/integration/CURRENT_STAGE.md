# 当前唯一阶段

NF 受限委托桥接 [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) → `main`。W2 于 2026-11-11T18:16:58Z 正式合并并完成全量主线回归。最新已验收 main 为 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。[原始证据和验收](W2_主线签收.md)

重新核对 #11 的 head/base、原始变更、桥接规格和历史资格验证；从最新 main 纳入仅属于 NF 受限委托的改动。验收真实 stdio 服务身份、跨主体隔离、预算、幂等重试与 UNKNOWN、数据库迁移和恢复边界。签署 PR 后合并并重新验收实际主线。冻结 W3–W6、P6；Scion 保持独立。

#11 已改投 W2 主线。候选 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 在原验收代码中补齐了安装到期时的预留和发送围栏。真实双服务、两处 WAL 落点和到期正反例已复验。核对本候选五项托管工作流及原始容量数据后再决定合并。[候选证据](evidence/NF-Bridge/candidate-26bf033/README.md)
