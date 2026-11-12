# PR 交付矩阵

| 阶段 | PR | 接管头 SHA | 接管目标 | 集成状态 |
|---|---:|---|---|---|
| P5.5 S0–S9 | [#6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) | `97eeceac266016b0d5da62786b019b704bfcdb70` | `main` | MERGED_AND_VERIFIED，main `df1e0cc904e3` |
| Workshop W0 | [#7](https://github.com/SKYdayDUCK/NovelFactory/pull/7) | `c18e53959807a35816eaa1258a3b7c64b80b4637` | `main` | MERGED_AND_VERIFIED，main `c4939e24b6b5` |
| Workshop W1 | [#8](https://github.com/SKYdayDUCK/NovelFactory/pull/8) | `f59ac5e442bd5a434ded2e65526d5750c5488e9b` | `dev/aw-w0` | MERGED_AND_VERIFIED，main `d039839fe68f` |
| Workshop W2 | [#9](https://github.com/SKYdayDUCK/NovelFactory/pull/9) | `0da5b7c8fab5321fa50d2a04a516f3fd95432539` | `dev/aw-w1` | MERGED_AND_VERIFIED，main `f2c584ec8e73` |
| NF 受限委托桥接 | [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) | `90cac947c6f1b176f20df6a579027f24e96d7b4c` | `dev/p5-5-redis-eventhub` | MERGED_AND_VERIFIED，main `15f606c99c76` |
| Workshop W3 | [#10](https://github.com/SKYdayDUCK/NovelFactory/pull/10) | `15d1cf51c357627866d06bd49fd326798dda56a3` | `dev/aw-w2` | MERGED_AND_VERIFIED，main `e6763d844f02` |
| Workshop W4 | [#12](https://github.com/SKYdayDUCK/NovelFactory/pull/12) | `71b471416c9407e348d4bdce817588def5f11d5b` | `dev/aw-w3` | MERGED_AND_VERIFIED，main `6c711adf7948` |
| Workshop W5 | [#14](https://github.com/SKYdayDUCK/NovelFactory/pull/14) | `609e92278476c60a893658ef53b6ff15f659b153` | `dev/aw-w4` | MERGED_AND_VERIFIED，main `b30e76978830` |
| Workshop W6 | [#15](https://github.com/SKYdayDUCK/NovelFactory/pull/15) | `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771` | `main` | **MERGED_AND_VERIFIED**, main `98a8bbed289a29d0326454c86dd3db54ac85ae5c`, AT-30 NOT_MET |
| P5.6 综合验收 | [#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) | `e5f5348ca07dc50c9fe52ab271a733ac31fa063c` | `main` | **MERGED_AND_VERIFIED**，[主线 `4e8fc5a`](https://github.com/SKYdayDUCK/NovelFactory/blob/3ef5a72d7a65edb76a988132a72c4e82ee0805ee/reports/integration/evidence/P56/main-4e8fc5a/SIGNOFF.md) |
| P6 本地验收 | [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) | `1d950705ccdebaca02171bd24293682f6efeab54` | `dev/aw-w6` | **MERGED_AND_VERIFIED**，main `0420356b0bd0bfd675494e581799946c4b967f6e` |
| P6 预算 | [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) | `43dc369ab73e2faacb509b6ca16995848fc92c28` | `main` | **MERGED_AND_VERIFIED**，main `533c1f526ef8841610d5c414e77ecbea8bbd5e35` |
| Scion 实验 | [#4](https://github.com/SKYdayDUCK/NovelFactory/pull/4) | `4f249d520c921c8d6effee9978f7338b912f1dd0` | `dev/p3-p4-continuity` | Open、Draft，原头与目标未变 |
| S9 历史实验 | [#18](https://github.com/SKYdayDUCK/NovelFactory/pull/18) | `3985ebd4f90ff9f7d2e9626df9c0fbf5e8cdb1be` | 历史分支 | ARCHIVED_AND_CLOSED，2026-11-12T04:42:53Z |
| 第一阶段门禁 | [#27](https://github.com/SKYdayDUCK/NovelFactory/pull/27) | `5aa9b71b66c7800f58214fa368e22ebeb9c3d56b` | `main` | **MERGED_AND_VERIFIED**，`276820316129d546b0a13118bcbe283b06507795` |

阶段状态详见 [交付台账](DELIVERY_LEDGER.md)，接管字段为历史快照。

#11 现以 `main` 为目标，候选 `26bf0337`，双库专项通过，托管验收进行中。[原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/candidate-26bf033/README.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](https://github.com/SKYdayDUCK/NovelFactory/blob/df8bea90fe19ea492220448e07323f63c4e496ea/reports/integration/evidence/NF-Bridge/main-15f606c/merge.json)


W3 已签收于 `e6763d844f023d7c6a13060dbaebca0416fe3099`，下一阶段为 W4。[记录](W3_主线签收.md)。

W6 #15 当前指向 main，接管时 Draft 头为 ac579e0b89b55e613d32750ce1c07210e0b07022。此次整理继承 34e4e7658b8a、73050eab6c5b 的已交付历史。正式合并和 main 验收仍待执行。

W6 #15 的当前正式候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`，树 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。GitHub 预合并 a1f1021a 与该候选同树，目标 W5 main 为 b30e7697。 PR 已准备审查。P6 #17、#16 和 P5.7 仍排在 W6 与 P5.6 主线综合签收之后。


W6 主线签收见 [W6_主线签收.md](W6_主线签收.md)，下一唯一阶段 P5.6 双服务全面验收。

[#25](https://github.com/SKYdayDUCK/NovelFactory/pull/25) 的合并提交为 `ba892d3a424be39efccb697232a5614b26609cc4`，2026-11-12 01:57:08 UTC 合并。已在[精确主线](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43137991893)复验并[签收](https://github.com/SKYdayDUCK/NovelFactory/tree/9eb7bb52e422f1a8e33a91fe01a1e67fef6375d3/reports/integration/evidence/W6/signoff-ba892d3)。 [P5.6 独立综合验收](P56_SYSTEM_ACCEPTANCE.md)。

#17 已于 2026-11-12 03:19:32 UTC 合并并在 `533c1f526ef8841610d5c414e77ecbea8bbd5e35` 验证。[#17 主线签收](https://github.com/SKYdayDUCK/NovelFactory/blob/fee462dfb6923059512b9797b4707e61eec8e8a3/reports/integration/evidence/P6-C1/main-533c1f5/SIGNOFF.md)。#16 在该基线上接收原堆叠分支 `bc7824078a7613c5e1a0d26031c3c64fa2db8490`。

#18 已归档关闭，原输入、测试版本和采用决策见 [S9 归档](evidence/S9-PR18/README.md)。[PRE_HARNESS_BASELINE](PRE_HARNESS_BASELINE.md)保留 #16 实际主线、开放列表和原始容量边界。
