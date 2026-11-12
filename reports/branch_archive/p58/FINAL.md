# P5.8-B 归档签收

P5.8-B 经 [PR #30](https://github.com/SKYdayDUCK/NovelFactory/pull/30) 合入 `f0ede9b44e44226e3dd6ff8254f451640cf318bf`。在 2026-11-12 的[远端执行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43227888297)中，GitHub 发布归档，工作流从 Release 下载原件，通过独立恢复后原子删除 81 条引用。远端由 91 个分支降为 10 个。本轮刚开始时为 89 个，随后新增本轮单一开发分支和一个 Scion 实验分支。

| 核对项目 | 实际结果 |
| --- | --- |
| 类别 A | 31 条由正式 A main 包含的分支全部清理。 |
| 类别 B | 50 条有独有历史的分支完整归档并清理。 |
| 类别 C | 10 条保留。每个分支的开放 PR、审查或运行依赖见[逐分支审计](BRANCHES.md)。 |
| 恢复 | 从公开 Release 下载的 bundle 为 139,621,401 字节，SHA-256 为 `0766cccec031f9117eaaa3af57f70da01fd8aae1e52c94744c2fe88b2a84859b`。固定的前提标签指向 A main `630c4bc`。删除后再次从空裸库按标签和 bundle 重建，91 个冻结 tip 全部匹配，完整 fsck 通过。 |
| 原 Scion 和 P5.7 审查 | 原 PR #4 在清理前后均为开放草稿，base `23a6139`、head `4f249d5`。P5.7 独立审查分支停在 `ece71f7`。 |
| Scion 新实验基线 | 实验分支 `experiment/scion-p58-compat` 指向 `75276a7`，直接继承 A main。120 项兼容回归和 6 个场景的三组 PostgreSQL 数据比对通过。[Scion 复验](scion/README.md) |

签收时的 10 个远端分支为 `main`、`dev/p58-agent-runtime`、`dev/p57-creative-harness-s1-s6`、`dev/p57-s0-architecture`、`dev/aw-w4`、`dev/p2-production-loop`、`dev/p3-p4-continuity`、`evidence/serial-delivery`、`experiment/scion-incremental-20261111`、`experiment/scion-p58-compat`。事务之后的证据提交会正常推进 main 和本轮开发分支。原始前后 SHA 以[实际事务回执](raw/p58-refs-43227888297-1.json)为准。

[归档 Release](https://github.com/SKYdayDUCK/NovelFactory/releases/tag/p58-history-4157e475a9b33ba2) 保留 bundle、机器清单、计划、恢复收据与完整的前后引用。按[恢复步骤](README.md)可在历史分支已删除的情况下取回任一原始提交、代码或实验文件。[发布资产摘要](raw/published-release.json)

删除后的[第三方下载和独立恢复记录](raw/independent-release-verification.json)包含实物校验、来源、裸库完整性和逐个引用的检查结果。[GitHub 归档恢复收据](raw/release-archive-receipt.json)
