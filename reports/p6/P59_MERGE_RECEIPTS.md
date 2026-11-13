# P5.9 两次正式合并与主线复验

| 步骤 | GitHub 记录 | 本次独立核验 |
|---|---|---|
| Runtime #33 | [PR](https://github.com/SKYdayDUCK/NovelFactory/pull/33) · [主线 3f8d3d3c](https://github.com/SKYdayDUCK/NovelFactory/commit/3f8d3d3cc630b261d7203f5b6a7809c0f7a097ee) · [merged 回执](evidence/p59-github-33.json) | 重核独立环境原始产物。500 个读取身份，3,600.0365 秒，72,000/72,000 次有效。500、1,000、2,000 个逻辑 Agent 分别产生 490、980、1,960 个预期有效结果，各有 1% 预设取消和 1% 预设 UNKNOWN。合并后运行 [PG/Runtime 回归](evidence/33-locked-pg-runtime.xml) 与 [跨入口测试](evidence/33-cross-entry.xml)。 |
| 文学方法 #34 | [PR](https://github.com/SKYdayDUCK/NovelFactory/pull/34) · [主线 bcb03637](https://github.com/SKYdayDUCK/NovelFactory/commit/bcb03637d4f01030fc95b99d1ff743b3758297ad) · [merged 回执](evidence/p59-github-34.json) | 合入 #33 后再同步候选。35 项改动全部是 Markdown，24 个受保护文件对象保持一致，66 个合法角色/任务/技能组合的指令与授权一致，123 处相对引用可解析。候选 [单元/契约](evidence/34-candidate-contracts.xml)、[PostgreSQL](evidence/34-candidate-pg.xml) 通过。实际合并后的主线 [119 项回归](evidence/34-main-pg.xml) 与 [设计包校验](evidence/34-main-package.json) 通过。 |

冻结主线与 #33 候选同条件测试，200 Agent 过载的首次调用分别为每秒 44.94585 与 45.20040，门槛为 950。四项历史红灯与冻结主线对照一致，GitHub 当时没有将这些列为受保护分支必需检查。[独立容量原始检查链接](evidence/p59-qualification.json) · [核对结果](evidence/serial-source-comparison.json) · [遗留问题 #13](https://github.com/SKYdayDUCK/NovelFactory/issues/13)

两次合并均已核实 merged=true，并安全归档对应旧开发分支。P6 分支从第二次合并 SHA 新建。旧文学作品在 13 份原始对账上仍有阻断，每书仅有一份旧评阅，内部正式发布为零。本轮 P6 签收记录只接收新作品原生证据。

实际合并提交的所有托管检查已结束。#33 主线共 27 项，23 项通过，包括一小时持续读取、全量 PostgreSQL 17 回归与 Windows/Linux 原生测试，4 项为保留的容量遗留。#34 主线共 20 项，17 项通过，3 项为历史容量遗留。[#33 主线逐项链接](evidence/postmain-33-ci.json) · [#34 主线逐项链接](evidence/postmain-34-ci.json)
