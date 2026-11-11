# 并发优化集成与 PR 审核

审查范围为 [PR #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)，基于 dev/p3-p4-continuity。保留 Draft，由独立审核者决定合并范围。A0、B、C、D 各自通过 Git Data API 构造 Git tree 和单父提交，以预期父 SHA 非强制更新同一分支。PR #2、#3 与 main 未执行写入。

| 检查点 | 远程提交 | 已验证的内容 |
|---|---|---|
| 阶段 A 原始工作 | [2c28acb](https://github.com/SKYdayDUCK/NovelFactory/commit/2c28acba6e1932657162c6e55b3288365ce3ea23) | 已有研究、真实 PG17 的修正仪表和基线 |
| A0 | [bc2c3b8](https://github.com/SKYdayDUCK/NovelFactory/commit/bc2c3b8bcbbd9d97ac5c875eaae94733d5e100e9) | 本地历史保全、断链及客户端安装修复。[工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42938464856) |
| B | [1db9921](https://github.com/SKYdayDUCK/NovelFactory/commit/1db99217497c74f0e6ebb6fdf8c4ed977022963f) | 三个独立默认关闭的开关，有限语义后缀、完整回退。[工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42938942422) |
| C | [a4c12be](https://github.com/SKYdayDUCK/NovelFactory/commit/a4c12be7df8548bbf7fd913bf2059cfd2b84cf3e) | 冻结原始实现、B 全关、B 全开三臂。[工程 CI](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42939422116) |
| D | 承载本报告的提交 | 性能证据、开关消融、集成决定与 CI 冻结版差分步骤；准确提交及本次 Actions 链接由 PR 正文记录。 |

## 正确性与审核范围

[技术设计](增量世界版本与状态投影_技术设计.md)、[三臂等价性报告](新旧世界内核_等价性验证.md)、[性能对照](Scion思路_并发优化实验.md)、[恢复取证](Scion_中断恢复与开发接管.md)。B 全开执行整个原仓库测试与 B 专项，在 PostgreSQL 17.11 上 646 项通过，单条警告来自 Starlette 测试客户端的依赖弃用通知。[本地执行日志](../evidence/scion_20261111/phase_d/all_switches_pg17.log) · [完整 JUnit](../evidence/scion_20261111/phase_d/all_switches_junit.xml.gz)

应用代码独立复核未发现已证实的权限绕过、状态分歧或漏掉正式提交检查。差分覆盖的是规定场景及字段。物理记账时间的四个列仅比较空值，其余持久化业务时间、证书、审批、回执及成功响应保留全值。审阅代码时，应分别核对 query_revision 的字段清单与调度投影校验、scope_versions_for 的 work/branch 限定与完整注册 footprint，以及 ReplayProofs 的父投影验证、内容摘要、历史顺序和故障回退。对于添加新模板或解释器，必须复查证明的前提及完整重放的错误行为。

## 集成决定

**A0 和回归证据**适合首先审核。A0 仅处理文档和真实 PG17 客户端安装，CI 继续校验 pg_dump、pg_restore、psql 三者主版本。C 的数据库克隆差分及 D 的可复现脚本作为审查工具留在 PR #4。工程 CI 在同一运行内读取冻结原版工作树，运行相同测试，再以其逐表和 API 返回证据检查候选。容量工作流另行按既有规则判定。

**B1 的派生工作者元数据复核与修订审核枚举**可以作为小范围集成候选。前者已在完整验证源之后只比较三个标量，200 人口夹具中一次复核的 JSON 解码由 662,894 字节降到 0；后者基于原先实际使用的两列，尚无独立的整次推广性能对照。固定 cut 查询只节省了 scope_versions 等未使用列，仍要读取和校验 snapshot。将三处用一个部署开关启用时，不能把工作者单条 SELECT 的收益归给在线查询；正式推广可先拆出只读元数据两处，在 PR #4 上独立审核。

**B2 稀疏 Scope 查询**可保留为单独的集成候选。证书的读写 token、负向与幻读守卫、审批、授权及 fencing 均在原调用点保留。在 536 个 scope 的世界中，11 个请求键实际返回 9 行；缺失的两键仍按版本 0 生成 token。受测 SQL 往返次数与原实现相同。该改动针对证书构造和复核，完整 Revision 写入继续需要全分支 scope 版本集合。启用范围及是否单独推广取决于原负载复测。

**B3 后缀重放**继续留作实验，默认关闭。命中时少做历史契约验证与效果重放，但仍读取并哈希旧事件、验证及复制父投影并写完整选择。冷进程、重启、驱逐和大量 fallback 会改变收益，长历史首写有回归。Retcon 和带复杂因果的正式接受必须继续走旧完整路径。将频繁变化的生产调用或多个进程的命中率从一个连续单进程样本外推，会高估收益。

## 回滚和分支边界

三个 NF_SCION 开关在 Kernel 创建时取值。停用对应开关并重建服务进程即可回原路径，既有事件、表结构和持久化格式兼容。部署后若引入新模板或发生未解释的差异，关闭相关开关，保留原事件、回执及可重建投影并用同一固定世界对照。无数据库逆迁移，无需改写 Git 历史。本轮没有修改容量阈值、生成器重试次数、连接预算、任务授权或正式分支锁。请求配额、真实模型与文学评价没有由确定性工程试验验收。

[A0、B、C 每个工作流和作业的终态清单](../evidence/scion_20261111/phase_d/checkpoint_ci.json)保留基础工程与容量的分别判定。三个基础 CI 均通过，PG17 主套件分别为 631、646、652 项通过。三轮各有一条测试客户端的依赖弃用警告。既有 P2/P3 压力工作流仍包含未达到原阈值的热点或开放到达场景；这些 CI 默认运行未启用实验开关的正常路径，候选性能以 D 的同环境配对为准。

PR #4 的代码、冻结版本、回归证据、同环境对照及回退步骤已具备独立审核条件。建议各开关分别审核，容量验收继续开放，默认开关维持关闭。
