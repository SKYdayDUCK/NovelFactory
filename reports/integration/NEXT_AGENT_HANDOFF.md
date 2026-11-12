# 工程签收与交接

本轮起点是 S0 主线 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b`。依次核对六个阶段和 [S6 签收](../harness/evidence/s6/receipt.json)。首先读取远端两条分支，根据主线合并的父提交、文件树和验证回执确定下一步。

若尚无正式合并，确认 main 最新基线，在候选合并上运行主线工程套件，通过后以非强制方式一次更新 main，再重新检出并验证。若已有合并，根据[开发分支主线签收与独立审核](https://github.com/SKYdayDUCK/NovelFactory/tree/dev/p57-creative-harness-s1-s6/reports/code_review/p57_creative_harness)继续尚未完成的复核或选择性修复。审查目录只保存在开发分支。[原始委托](P57_SINGLE_BRANCH_MANDATE.md)
