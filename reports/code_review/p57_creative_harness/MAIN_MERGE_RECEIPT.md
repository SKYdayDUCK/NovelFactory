# 主线合并、修复挑选和远端签收

## 首次合并

| 事项 | 不可变标识或实际观察 |
| --- | --- |
| S0 / 首次合并第一父 | `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b` |
| 原开发分支 CODE_TIP_SHA / 第二父 | `602ba7fe8ece18503ac3b994adee4f00634cd761` |
| 正式 `git merge --no-ff` 合并 | `1de471076b8fb4240f95ff8033c60db34c187404` |
| 合并树 / S6 树 | `3fe25fcb17d2fd76618a0695b1d54641d71ce60f` |
| 初次范围 | 183 files changed, 24,684 insertions, 149 deletions |
| 名称清单 SHA-256 | `6a4cd0c0e8f2d4d0268d714178ba09eb94cded9999ec223fdb080c9f1d05f0eb` |
| 完整二进制 diff SHA-256 | `71f07b1feee059a0392801c9e868ddac31fb8d75d1d43c7cf859b7f1ade7bb89` |
| 首次发布后远端回读 | 2026-11-12 10:10:20 UTC，正式 main 的两个父与文件树均一致，审查目录缺席 |
| 审查启动 | 2026-11-12 10:13:24 UTC，首次合并远端专项于 10:13:21 UTC 成功 |
| 七份独立初稿 | `5dadfc4ff36088f6bd31b324acb7fb673891b7bb`，保存于开发分支 |

原生 Git 运行器从两个实际父提交重新合并，对照整树、全量 diff 与文件清单。工程、包安装及补充套件均在首次发布前完成。[非快进重建](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197297588/job/131111401917) · [初次候选工程与包](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197297180) · [初次候选补充组](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197297100) · [首次正式主线专项](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764)

| 里程碑 | 开发提交 |
| --- | --- |
| S1 | [2d47a66f297dd017c6bac3f207abb6f6752cc742](https://github.com/SKYdayDUCK/NovelFactory/commit/2d47a66f297dd017c6bac3f207abb6f6752cc742) |
| S2 | [32ef8b3ced7fb5ea06cb25ecd4ccfa436fe9150b](https://github.com/SKYdayDUCK/NovelFactory/commit/32ef8b3ced7fb5ea06cb25ecd4ccfa436fe9150b) |
| S3 | [7a480d0371622966520f354c19e80257465398b9](https://github.com/SKYdayDUCK/NovelFactory/commit/7a480d0371622966520f354c19e80257465398b9) |
| S4 | [7e8659f286407b0428a37f3fae8e9c6905e7c203](https://github.com/SKYdayDUCK/NovelFactory/commit/7e8659f286407b0428a37f3fae8e9c6905e7c203) |
| S5 | [976e3212fcb42b9251fc2858a3b9d9ce3c3a646c](https://github.com/SKYdayDUCK/NovelFactory/commit/976e3212fcb42b9251fc2858a3b9d9ce3c3a646c) |
| S6 | [602ba7fe8ece18503ac3b994adee4f00634cd761](https://github.com/SKYdayDUCK/NovelFactory/commit/602ba7fe8ece18503ac3b994adee4f00634cd761) |

## 首次合并的主线作业

| 范围 | 事实 |
| --- | --- |
| Harness / 双服务 / 三臂 / 物理恢复 | [正式合并五项成功](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896764) |
| 原有 P1/P2 生产完整性 | 932 passed、18 skipped、17 subtests，作业通过。[完整性测试](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896670/job/131113457488) |
| 物理恢复及 P6 离线授权 | [P4 物理演练通过](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896657) · [P6 第二次运行通过](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896593)。P6 首次因 Ubuntu 软件镜像连接超时，恢复尚未开始。 |
| Workshop 主工作流 | 既有容量/场景两个作业通过，常规集成 9 failed / 5 passed。作为 P57-TC-01 本轮修复。[首次失败](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896850/job/131113456793) |
| 未达标的容量工作流 | [P2 50/200 overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896465) · [P3 1000-read-open](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896540) · [P3/P4 200 overload](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896530)。S0 对照作业见下节。 |

## 主线选择性修复

逐一从开发分支选入的 12 个代码、测试和 CI 修复有 12 对 SHA，见 [修复对应表](REMEDIATION.md)。独立运行器从原合并开始实际执行每项 `git cherry-pick --no-commit`，核对 Git 树和二进制补丁，逐次与单父主线提交相符。修复后共有 19 个文件变化，增加 862 行，删除 120 行。补丁不含审查文档。[12 项选择性重放](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324524/job/131132080228)

| 事项 | 值 |
| --- | --- |
| 选择性更新的远端原值 | `1de471076b8fb4240f95ff8033c60db34c187404` |
| 最终正式 main | `d6ff88223d6ae655a45d713687e5e8119e157737` |
| 最终正式树 | `eeba8d062d6d8aeb9628f60b564fe9653443a05c` |
| 修复范围 patch SHA-256 | `45f3fe62c3de88107c59597a3074dea3342846b31a679e7fc6e8df5d43675478` |
| 修复范围名称清单 SHA-256 | `7077bbcb58836913b0d715c17d2769fff74a9493f2cf1270ea12f553b993b1ea` |
| 更新及回读 | 10:56:10 UTC 以原合并为期望值完成非强制快进，10:56:25 UTC 由 GitHub branch、Git ref、commit 三个入口取得相同主线 SHA |
| 主线文件树 | 本轮审查目录、当时的 15 个临时验证文件以及之后用于观察空意见的开发临时工作流均缺席。全部 19 个正式修复路径与保留开发分支的产品文件逐 blob 一致。 |

最后一次推送前的候选全量资格包括五项主工程作业、清洁环境双 wheel / NF sdist 打包、76 项 Harness、14 项双服务、真实三臂、物理 WAL/PITR，加上合约 432（另 17 个 subtest）、故障/并发 105、升级 7 与 17、P6 3、双向恢复 1、历史角色流程 96、查询与服务 59。组间重叠。打包第一次重放载体缺少临时验证脚本，其余四类工程套件通过，补齐验证脚本后整组重跑成功。[最终候选全部通过](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204125409) · [补充 8 组](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324863) · [首个打包载体失败](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324669/job/131132080795)

## 修复后实际远端 main

| 远端工作流 | 终态 | 实际结果 |
| --- | --- | --- |
| [Creative Harness engineering](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204722942) | SUCCESS | 5/5 作业成功，真实 PG Harness 76 passed，双服务 14 passed，三臂 1 passed，S0 静态与 WAL/PITR 通过。 |
| [P1 and P2 production integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204723031) | SUCCESS | 940 passed, 18 skipped, 2 warnings, 17 subtests passed in 993.82s (0:16:33)。Schema/domain/HTTP 与真实 PostgreSQL 的全量集成、并发和恢复作业均成功。 |
| [P2 measured capacity and recovery](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204722952) | FAILURE | 50 与 200 agents overload 两个原门槛未过，其他作业通过。200 热点两个测量约 48.04/50.56 次每秒，门槛 950，正确性与有界恢复通过。 |
| [P3 scale experiments with strict unmet gates](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204723111) | FAILURE | Measured 1000-read-open 未过，5000 次中 2481 次 HTTP 200、2514 次 429、5 次传输错误，52.37 次请求头启动每秒，门槛 950。500 持续读 600.004 秒、12000/12000 个 HTTP 200，一小时长测未执行。 |
| [Agent Workshop PostgreSQL and stdio](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204723135) | SUCCESS | 3/3 作业成功。常规 workshop/integration 17 passed，W6 message capacity 与 matched admission 两个场景作业均成功。独立 PostgreSQL 系统标识与强制命令 SSH 双代理回执有效，模型轮次 0。 |
| [P3 P4 measured evidence](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43204723460) | FAILURE | Independent load process / 200 / overload 原门槛未过，其他作业通过。与 S0 同名同类型。 |

原 S0 主线的同类容量作业亦报告相同门槛失败。[S0 P2](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43166232934) · [S0 P3](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43166232916) · [S0 P3/P4](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43166233070) · [Issue #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)

正式产品修复 SHA 下归档了第二份完整合成对照，文件 Git blob 为 `f6750235dce37f1ef5559ef46fa7d283554b9e64`，文件 SHA-256 为 `6c039e6d34cc1190b95bde25b073be7d4fd99adb6f4f8f72859224cb04c227d2`，3/3 窗口、9 次合成 Attempt、0 商业调用。[最终修复树的原始输出](../../harness/evidence/s6/post-review-three-arm-comparison.json) · [S6 阶段原始输出](../../harness/evidence/s6/three-arm-comparison.json)

## 审查提交与分支

正式产品由 `main` 保存。15 份最终审核文件保存于 `dev/p57-creative-harness-s1-s6` 的 `reports/code_review/p57_creative_harness/`。首个包含 15 份定稿的提交用 `git log --diff-filter=A --format=%H -- reports/code_review/p57_creative_harness/FINAL_VERDICT.md` 精确定位。将此 SHA 及远端文件树复核计入紧随其后的签收提交，以避免提交引用自身。[开发分支审查目录](https://github.com/SKYdayDUCK/NovelFactory/tree/dev/p57-creative-harness-s1-s6/reports/code_review/p57_creative_harness)

最终开放 PR 只有原有的 Scion draft #4。本轮没有创建 PR，也没有第二次合并开发分支。[Scion #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)
