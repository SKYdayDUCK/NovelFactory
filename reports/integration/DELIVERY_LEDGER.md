# 串行交付台账

| 阶段 | PR | 存证状态 | 实际 main SHA 与时间 |
|---|---|---|---|
| P5 | [#5](https://github.com/SKYdayDUCK/NovelFactory/pull/5) | 历史合并已核实；工程/原生桌面/恢复的合并依据见 #5。既有 AT-30 保留 | `a8f37b72286163e9117f8874a0e175c61204ff8b`，2026-11-11 13:50:15 UTC |
| P5.5 | [#6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) | MERGED_AND_VERIFIED；[主线用例、PITR、托管作业及容量限制](p55-main/README.md) | `df1e0cc904e3b94a360d86e3a3e00bfec3258673`，2026-11-11 16:33:42 UTC |
| W0 | [#7](https://github.com/SKYdayDUCK/NovelFactory/pull/7) | MERGED_AND_VERIFIED；[W0 主线签收](W0_主线签收.md) | `c4939e24b6b5501d82127f528f2054e9c1992856`，2026-11-11 17:11:10 UTC |
| W1 | [#8](https://github.com/SKYdayDUCK/NovelFactory/pull/8) | MERGED_AND_VERIFIED；[主线签收](W1_主线签收.md) | `d039839fe68f6403ea6e5590af3ed787318d5b5f`，2026-11-11T17:48:11Z |
| W2 | [#9](https://github.com/SKYdayDUCK/NovelFactory/pull/9) | MERGED_AND_VERIFIED；[主线签收](W2_主线签收.md) | `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`，2026-11-11T18:16:58Z |
| NF Bridge | [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) | MERGED_AND_VERIFIED；[主线签收](NF_Bridge_主线签收.md) | `15f606c99c765deeffd904ad540441eb6476b3af`，2026-11-11T19:02:35Z |
| W3 | [#10](https://github.com/SKYdayDUCK/NovelFactory/pull/10) | 唯一开放的阶段 | 待正式合并 |
| W4–W6、P6 | [矩阵](PR_MERGE_MATRIX.md) | 冻结 | 待前一阶段关闭 |

每阶段的原 PR 讨论保存合并后的完整 SHA、时间、主线验收作业、遗留问题和下一阶段是否开放。后续阶段 PR 将前一阶段已确认的回执归入本表，保留原 PR 上可即时复查的记录。

#11 已改投 W2 主线。候选 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 在原验收代码中补齐了安装到期时的预留和发送围栏。真实双服务、两处 WAL 落点和到期正反例已复验。核对本候选五项托管工作流及原始容量数据后再决定合并。[候选证据](https://github.com/SKYdayDUCK/NovelFactory/blob/e8529cd74cef2b8cf556396da8006409eeda66b0/reports/integration/evidence/NF-Bridge/candidate-26bf033/README.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/e8529cd74cef2b8cf556396da8006409eeda66b0/reports/integration/evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/e8529cd74cef2b8cf556396da8006409eeda66b0/reports/integration/evidence/NF-Bridge/main-15f606c/merge.json)

W3 已在 #10 接续已签收主线。真实 `w003`、`w003a` 已部署数据库升级到 `w003b`，并复演双服务回执遗失与两方向物理 WAL。[预演环境及原始数据](https://github.com/SKYdayDUCK/NovelFactory/tree/aecc83aa98345d5f153b104203d542de56be1c15/reports/integration/evidence/W3/preflight-d8c4e97-nf15f606c)。最终签收待合并后复验。
