# 当前唯一阶段

NF 受限委托桥接 [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) → `main`。W2 于 2026-11-11T18:16:58Z 正式合并并完成全量主线回归。最新已验收 main 为 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`。[原始证据和验收](W2_主线签收.md)

重新核对 #11 的 head/base、原始变更、桥接规格和历史资格验证；从最新 main 纳入仅属于 NF 受限委托的改动。验收真实 stdio 服务身份、跨主体隔离、预算、幂等重试与 UNKNOWN、数据库迁移和恢复边界。签署 PR 后合并并重新验收实际主线。冻结 W3–W6、P6；Scion 保持独立。

目前的唯一活动 PR 为 #11，原两父整合头 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c`。候选更新采用 [该 PR](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 公布的最新冻结 SHA。真实资格验证、旧升级失败与历史负载原件见 [桥接验收表](NF_Bridge_契约验收.md)。完成所有适用候选检查、核对 500 持续读与历史容量限制后，经正常合并并验证实际 main；W3 尚未获准启动。
