# 当前唯一阶段

**Workshop W0，PR [#7](https://github.com/SKYdayDUCK/NovelFactory/pull/7) → main。** P5.5 已完成主线验收，main 为 `df1e0cc904e3b94a360d86e3a3e00bfec3258673`。W0 开放时的原分支头为 `c18e53959807a35816eaa1258a3b7c64b80b4637`，已核实整合两父的工作提交为 `df037971c6f1719facf0d2b51dd161945d152afc`；发布后的候选及作业按 PR 的 head SHA 核对。

- P5.5 的候选 `67e8836fd8aceb5d4b8d054594f5f3bdfdddaac9` 已随 [#6](https://github.com/SKYdayDUCK/NovelFactory/pull/6) 合并。主线验收、原始文件与未达标容量见 [签收回执](p55-main/README.md)。
- W0 复用现有双服务 stdio、身份安装、会话和发现测试。从上述 main 整理 #7 的独有差异，校验各角色实际可发现、可调用及拒绝的工具；用真实 PostgreSQL 验证安装、绑定、撤权和服务身份隔离。
- W0 尚未合并。在 #7 候选定版后读回 GitHub 门禁，经正常 PR 流程合并并验证实际 main。W1 及之后均冻结。
