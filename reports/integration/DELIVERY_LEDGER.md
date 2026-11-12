# 串行交付台账

| 阶段 | PR | 存证状态 | 实际 main SHA 与时间 |
|---|---|---|---|
| P5 | [#5](https://github.com/SKYdayDUCK/NovelFactory/pull/5) | 历史合并已核实；工程/原生桌面/恢复的合并依据见 #5。既有 AT-30 保留 | `a8f37b72286163e9117f8874a0e175c61204ff8b`，2026-11-11 13:50:15 UTC |
| P5.5 | [#6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) | MERGED_AND_VERIFIED；[主线用例、PITR、托管作业及容量限制](p55-main/README.md) | `df1e0cc904e3b94a360d86e3a3e00bfec3258673`，2026-11-11 16:33:42 UTC |
| W0 | [#7](https://github.com/SKYdayDUCK/NovelFactory/pull/7) | MERGED_AND_VERIFIED；[W0 主线签收](W0_主线签收.md) | `c4939e24b6b5501d82127f528f2054e9c1992856`，2026-11-11 17:11:10 UTC |
| W1 | [#8](https://github.com/SKYdayDUCK/NovelFactory/pull/8) | MERGED_AND_VERIFIED；[主线签收](W1_主线签收.md) | `d039839fe68f6403ea6e5590af3ed787318d5b5f`，2026-11-11T17:48:11Z |
| W2 | [#9](https://github.com/SKYdayDUCK/NovelFactory/pull/9) | MERGED_AND_VERIFIED；[主线签收](W2_主线签收.md) | `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`，2026-11-11T18:16:58Z |
| NF Bridge | [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) | MERGED_AND_VERIFIED；[主线签收](NF_Bridge_主线签收.md) | `15f606c99c765deeffd904ad540441eb6476b3af`，2026-11-11T19:02:35Z |
| W3 | [#10](https://github.com/SKYdayDUCK/NovelFactory/pull/10) | MERGED_AND_VERIFIED，[主线签收](W3_主线签收.md) | `e6763d844f023d7c6a13060dbaebca0416fe3099`，2026-11-11T19:53:57Z |
| W4 | [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) | MERGED_AND_VERIFIED，[W4 主线签收](W4_主线签收.md) | `6c711adf794842bdf4469ab51e89093d37761bcc`，2026-11-11T21:42:09Z |
| W5 | [#14](https://github.com/SKYdayDUCK/NovelFactory/pull/14) | MERGED_AND_VERIFIED，[W5 主线签收](W5_主线签收.md) | `b30e76978830bec4c596c04194d749fd01f67402`，2026-11-11T22:31:34Z |
| W6 | [#15](https://github.com/SKYdayDUCK/NovelFactory/pull/15) | 当前唯一开发阶段 | 待主线签收 |
| P5.6 全验、P6、P5.7 | [矩阵](PR_MERGE_MATRIX.md) | 串行等待 | 按上一阶段签收开放 |

每阶段的原 PR 讨论保存合并后的完整 SHA、时间、主线验收作业、遗留问题和下一阶段是否开放。后续阶段 PR 将前一阶段已确认的回执归入本表，保留原 PR 上可即时复查的记录。

#11 已改投 W2 主线。候选 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 在原验收代码中补齐了安装到期时的预留和发送围栏。真实双服务、两处 WAL 落点和到期正反例已复验。核对本候选五项托管工作流及原始容量数据后再决定合并。[候选证据](evidence/NF-Bridge/candidate-26bf033/README.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](evidence/NF-Bridge/main-15f606c/merge.json)


W3 已签收。真实双服务委托与交接、预算与恢复均通过，两个托管工作流完成。[记录](W3_主线签收.md)。

W6 在两个主管已合法调任的 PostgreSQL 用例中，一例新岗位、一例已停岗位，旧版扩编预览都未拒绝。接入现有岗位矩阵后，18 项预设、路由、通信与租期用例通过。[红绿 JUnit、复演脚本与 633cf1a 补丁](evidence/W6/preset-supervisor/W6-preset-supervisor-633cf1a.zip)。

W6 #15 的当前正式候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`，树 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。GitHub 预合并 a1f1021a 与该候选同树，目标 W5 main 为 b30e7697。八库升级、真实重启、NF 原生 10 项、四预设场景、AT43/49、17 项原生角色和 wheel 安装均已在精确候选取得独立回执。[八库与 NF](evidence/W6/candidate-50c8c5e/database-upgrade-and-fixtures/) · [首轮回归负控](evidence/W6/candidate-23d195d/creative-and-roles/broker-poll-timing.zip)。


## 2026-11-12 W6 签收 PR

[#25](https://github.com/SKYdayDUCK/NovelFactory/pull/25) 合并至 `ba892d3a424be39efccb697232a5614b26609cc4`，候选 `e6f79f1063fd52acbaefd20dedd5a1f186b3168b`，合并时间 01:57:08 UTC。原始合并记录、候选工件摘要与主线静态验证归入 [#25 证据](evidence/W6/signoff-ba892d3/pr-25.json)。主线 push 两作业于 02:10:07 UTC 完成，均成功。**MERGED_AND_VERIFIED**。[主线回执](evidence/W6/signoff-ba892d3/main-push.json)。署名范围和后续阶段以[本次十六节委托](CREATIVE_HARNESS_DELIVERY_SCOPE.md)为准。

P5.6 [#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) 在 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc` 完成主线复验。**MERGED_AND_VERIFIED**，保留历史 AT-30 NOT_MET、托管宿主跳过及真实创作授权边界。[P5.6 综合验收签收](evidence/P56/main-4e8fc5a/SIGNOFF.md)。
