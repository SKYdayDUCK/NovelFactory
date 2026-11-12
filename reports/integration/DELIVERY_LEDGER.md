# 串行交付台账

S6 观测、三组工程对照、真实服务及恢复验收已通过。[逐项回执](../harness/evidence/s6/receipt.json)

S5 已推送为 `976e3212fcb42b9251fc2858a3b9d9ce3c3a646c`，远端检出及 Actions 成功。[远端回执](../harness/evidence/s5/remote-receipt.json)

S5 指令根因、受控对照、独立发布和回滚 已在开发分支验证并进入阶段提交。[S5 工程回执](../harness/evidence/s5/receipt.json)

S4 动态角色、Skill 版本与代际恢复已推送至开发分支，提交为 `7e8659f286407b0428a37f3fae8e9c6905e7c203`。[远端 Actions 回执](../harness/evidence/s4/remote-receipt.json)。[S4 工程回执](../harness/evidence/s4/receipt.json)

S3 细粒度文学审核及定向修订已推送至开发分支，提交为 `7a480d0371622966520f354c19e80257465398b9`。[远端 Actions 回执](../harness/evidence/s3/remote-receipt.json)。[S3 工程回执](../harness/evidence/s3/receipt.json)

S2 剧情规划与隔离推演已推送至开发分支，提交为 `32ef8b3ced7fb5ea06cb25ecd4ccfa436fe9150b`。[远端 Actions 回执](../harness/evidence/s2/remote-receipt.json)。[S2 工程回执](../harness/evidence/s2/receipt.json)

2026-11-12 接管核实 S0 主线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`。接管记录已推送（1ef78e6b6a4a22339508de52a854e11a4be65bec）。S1 已推送至开发分支，提交为 `2d47a66f297dd017c6bac3f207abb6f6752cc742`（[远端和物理恢复回执](../harness/evidence/s1/remote-receipt.json)），见 [S1 测试及产品文件摘要](../harness/evidence/s1/receipt.json)。 [本轮二十节委托](P57_SINGLE_BRANCH_MANDATE.md) · [现场记录](../harness/S1_RECOVERY.md)


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
| W6 | [#15](https://github.com/SKYdayDUCK/NovelFactory/pull/15) | **MERGED_AND_VERIFIED** scoped W6, AT-30 NOT_MET; [主线签收](W6_主线签收.md) | `98a8bbed289a29d0326454c86dd3db54ac85ae5c`, 2026-11-11T23:47:40Z |
| P5.6 双服务综合验收 | [#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) | **MERGED_AND_VERIFIED**，[主线签收](https://github.com/SKYdayDUCK/NovelFactory/blob/3ef5a72d7a65edb76a988132a72c4e82ee0805ee/reports/integration/evidence/P56/main-4e8fc5a/SIGNOFF.md) | `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc`，2026-11-12 02:36:05 UTC |
| P6 全局 Token 预算 | [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) | **MERGED_AND_VERIFIED**，[#17 主线签收](https://github.com/SKYdayDUCK/NovelFactory/blob/fee462dfb6923059512b9797b4707e61eec8e8a3/reports/integration/evidence/P6-C1/main-533c1f5/SIGNOFF.md) | `533c1f526ef8841610d5c414e77ecbea8bbd5e35`，2026-11-12 03:19:32 UTC |
| P6 本地预检和部署 | [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) | **MERGED_AND_VERIFIED**，[#16 签收](https://github.com/SKYdayDUCK/NovelFactory/blob/a9d77b86c425de3cfe67c1b82996377003ac4200/reports/integration/evidence/P6-C2/main-0420356/SIGNOFF.md) | `0420356b0bd0bfd675494e581799946c4b967f6e`，2026-11-12 04:08:54 UTC |
| 存量归档与第一阶段门禁 | [#27](https://github.com/SKYdayDUCK/NovelFactory/pull/27) | **MERGED_AND_VERIFIED**，[PRE_HARNESS_BASELINE](PRE_HARNESS_BASELINE.md) | `276820316129d546b0a13118bcbe283b06507795`，2026-11-12T05:13:37Z |

每阶段的原 PR 讨论保存合并后的完整 SHA、时间、主线验收作业、遗留问题和下一阶段是否开放。后续阶段 PR 将前一阶段已确认的回执归入本表，保留原 PR 上可即时复查的记录。

#11 已改投 W2 主线。候选 `26bf0337b7a90f8e138fa77c09ce5339bb115e3c` 在原验收代码中补齐了安装到期时的预留和发送围栏。真实双服务、两处 WAL 落点和到期正反例已复验。核对本候选五项托管工作流及原始容量数据后再决定合并。[候选证据](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/candidate-26bf033/README.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/main-15f606c/merge.json)


W3 已签收。真实双服务委托与交接、预算与恢复均通过，两个托管工作流完成。[记录](W3_主线签收.md)。

W6 在两个主管已合法调任的 PostgreSQL 用例中，一例新岗位、一例已停岗位，旧版扩编预览都未拒绝。接入现有岗位矩阵后，18 项预设、路由、通信与租期用例通过。[红绿 JUnit、复演脚本与 633cf1a 补丁](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/W6/preset-supervisor/W6-preset-supervisor-633cf1a.zip)。

W6 #15 的当前正式候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`，树 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。GitHub 预合并 a1f1021a 与该候选同树，目标 W5 main 为 b30e7697。八库升级、真实重启、NF 原生 10 项、四预设场景、AT43/49、17 项原生角色和 wheel 安装均已在精确候选取得独立回执。[八库与 NF](https://github.com/SKYdayDUCK/NovelFactory/tree/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/W6/candidate-50c8c5e/database-upgrade-and-fixtures) · [首轮回归负控](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/W6/candidate-23d195d/creative-and-roles/broker-poll-timing.zip)。


W6 main 六条正式 push 工作流三绿三红：Workshop、NF integrity、PITR 成功；P2/P3 历史 AT-30 原容量门槛失败保留。[原始主线回执](W6_主线签收.md)。

[#25](https://github.com/SKYdayDUCK/NovelFactory/pull/25) 的合并提交为 `ba892d3a424be39efccb697232a5614b26609cc4`，2026-11-12 01:57:08 UTC 合并。已在[精确主线](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43137991893)复验并[签收](https://github.com/SKYdayDUCK/NovelFactory/tree/9eb7bb52e422f1a8e33a91fe01a1e67fef6375d3/reports/integration/evidence/W6/signoff-ba892d3)。 [P5.6 独立综合验收](P56_SYSTEM_ACCEPTANCE.md)。

#18 已归档关闭，原输入、测试版本和采用决策见 [S9 归档](evidence/S9-PR18/README.md)。[PRE_HARNESS_BASELINE](PRE_HARNESS_BASELINE.md)保留 #16 实际主线、开放列表和原始容量边界。
