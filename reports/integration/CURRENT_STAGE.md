# 当前唯一阶段

NF 受限委托桥接 [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) → `main`。W2 于 2026-11-11T18:16:58Z 正式合并并完成全量主线回归。最新已验收 main 为 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。[原始证据和验收](W2_主线签收.md)

重新核对 #11 的 head/base、原始变更、桥接规格和历史资格验证；从最新 main 纳入仅属于 NF 受限委托的改动。验收真实 stdio 服务身份、跨主体隔离、预算、幂等重试与 UNKNOWN、数据库迁移和恢复边界。签署 PR 后合并并重新验收实际主线。冻结 W3–W6、P6；Scion 保持独立。

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](evidence/NF-Bridge/candidate-695668b/README.md)
