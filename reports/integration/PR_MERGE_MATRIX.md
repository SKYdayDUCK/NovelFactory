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
| Workshop W6 | [#15](https://github.com/SKYdayDUCK/NovelFactory/pull/15) | `a0d8c7e0ac4e775fdb3190af6f254aecac0ca256` | `dev/aw-w5` | 当前唯一开发阶段，目标 main |
| P6 本地验收 | [#16](https://github.com/SKYdayDUCK/NovelFactory/pull/16) | `1d950705ccdebaca02171bd24293682f6efeab54` | `dev/aw-w6` | 冻结 |
| P6 预算 | [#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) | `43dc369ab73e2faacb509b6ca16995848fc92c28` | `dev/nf-workshop-delegation` | 冻结 |
| Scion 实验 | [#4](https://github.com/SKYdayDUCK/NovelFactory/pull/4) | `4f249d520c921c8d6effee9978f7338b912f1dd0` | `dev/p3-p4-continuity` | 冻结 |

阶段状态详见 [交付台账](DELIVERY_LEDGER.md)，接管字段为历史快照。

#11 现以 `main` 为目标，候选 `26bf0337`，双库专项通过，托管验收进行中。[原始回执](evidence/NF-Bridge/candidate-26bf033/README.md)

NF Bridge #11 的公开候选现为 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。实际 PostgreSQL 双服务专项 23 项全部通过。等待此候选的完整工程和原条件容量回执。[候选专项、WAL、权限和工作流](evidence/NF-Bridge/candidate-695668b/README.md)

NF Bridge [#11](https://github.com/SKYdayDUCK/NovelFactory/pull/11) 已于北京时间 2026-11-12 03:02:35 合入 `main`。合并提交为 `15f606c99c765deeffd904ad540441eb6476b3af`，与合格候选 `695668b8` 同树。当前阶段为 `MERGED_AWAITING_MAIN_VERIFICATION`。[合并双亲、树及两轮工作流](evidence/NF-Bridge/main-15f606c/merge.json)


W3 已签收于 `e6763d844f023d7c6a13060dbaebca0416fe3099`，下一阶段为 W4。[记录](W3_主线签收.md)。

W6 #15 当前指向 main，接管时 Draft 头为 ac579e0b89b55e613d32750ce1c07210e0b07022。此次整理继承 34e4e7658b8a、73050eab6c5b 的已交付历史。正式合并和 main 验收仍待执行。

W6 #15 的当前正式候选为 `50c8c5ed36cc2cfb83d6624c65ed33336e0f1771`，树 `7053bf12cc789dc6bf4220d0217a93253a2b75f9`。GitHub 预合并 a1f1021a 与该候选同树，目标 W5 main 为 b30e7697。 PR 已准备审查。P6 #17、#16 和 P5.7 仍排在 W6 与 P5.6 主线综合签收之后。


## 2026-11-12 W6 签收 PR

[#25](https://github.com/SKYdayDUCK/NovelFactory/pull/25) 合并至 `ba892d3a424be39efccb697232a5614b26609cc4`，候选 `e6f79f1063fd52acbaefd20dedd5a1f186b3168b`，合并时间 01:57:08 UTC。原始合并记录、候选工件摘要与主线静态验证归入 [#25 证据](evidence/W6/signoff-ba892d3/pr-25.json)。主线 push 两作业于 02:10:07 UTC 完成，均成功。**MERGED_AND_VERIFIED**。[主线回执](evidence/W6/signoff-ba892d3/main-push.json)。署名范围和后续阶段以[本次十六节委托](CREATIVE_HARNESS_DELIVERY_SCOPE.md)为准。

P5.6 [#26](https://github.com/SKYdayDUCK/NovelFactory/pull/26) 在 `4e8fc5a3c2331a0de669ad8851d82c5b08a1e5dc` 完成主线复验。**MERGED_AND_VERIFIED**，保留历史 AT-30 NOT_MET、托管宿主跳过及真实创作授权边界。[P5.6 综合验收签收](evidence/P56/main-4e8fc5a/SIGNOFF.md)。

[#17](https://github.com/SKYdayDUCK/NovelFactory/pull/17) 原 PR 的最终基线为 main，候选 `f96d26bcf449d8e7905099402ddb4ddcf8679a60`，正式合并 `533c1f526ef8841610d5c414e77ecbea8bbd5e35`。[P6 全局预算主线签收](evidence/P6-C1/main-533c1f5/SIGNOFF.md)。当前交付 #16。

#16 原 PR 位于 `main`，冻结 `6c01eee41f0366dd9a136038161b2d030d07ab67`，合并主线 `0420356b0bd0bfd675494e581799946c4b967f6e`，已复验。[精确签收](evidence/P6-C2/main-0420356/SIGNOFF.md)。
