# P5.7 Creative Harness 独立代码审核

本目录审核 NovelFactory 的 S1–S6 交付，以及这些改动与既有 NF Core、Agent Workshop、MCP 和持久化入口的相互作用。最初审核的范围为 `1cce11b5f0ead0d6ba4d57658a5ff5533b2ef37b` 至主线合并 `1de471076b8fb4240f95ff8033c60db34c187404`，其中产品代码 tip 为 `602ba7fe8ece18503ac3b994adee4f00634cd761`。完整差异涉及 183 个文件。修复后的主线产品提交是 `d6ff88223d6ae655a45d713687e5e8119e157737`。[首次合并的实际 Git 范围](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197297588/job/131111401917)

## 谁在何时进行了审核

2026-11-12 10:13:21 UTC，首次合并的远端 `main` 专项工程任务结束。10:13:24 UTC 起启用三个没有实施会话历史的新审查上下文，分别负责架构与权限、数据与并发成本、Harness 与文学及测试。三个上下文从固定 SHA 阅读生产代码、调用端、迁移、旧系统接口及测试，并生成七份独立原始报告。原始报告在开发分支的 `5dadfc4ff36088f6bd31b324acb7fb673891b7bb` 固定。某些缺陷后来由参与代码分析的上下文起草修复，发布协调者负责独立运行原树与修复树、逐补丁重放、复核两条远端引用。另一个初始审核上下文交叉读回了 S1、S5 及终审者身份修复。

上述独立性来自工作上下文和原始证据隔离。审核者属于同一模型家族，没有独立人类文学评委、线下身份调查或现场灾备操作员参与。七份原始报告保留复现前的判断和当时状态。各问题的最新状态见 [修复台账](REMEDIATION.md)。

## 环境与方法

GitHub 只读检出及托管 Linux 运行器、Python 3.12 锁文件、PostgreSQL 17、Redis 8、两个正式 stdio 服务、原生隔离的角色进程。审查追踪权限及写路径，比较 NF 和 Workshop 系统标识、数据库迁移与历史记录，检查冻结源、制品哈希、真实进程资格、预算 UNKNOWN 和回滚。原始回执、修复前后测试、专用工作流和已有主线测试均按各自计数口径引用。外部商业调用次数为 0。

## 阅读索引

| 文件 | 内容 |
| --- | --- |
| [REVIEW_SUMMARY.md](REVIEW_SUMMARY.md) | 总体判断、风险排序、部署范围和证据边界 |
| [ARCHITECTURE_REVIEW.md](ARCHITECTURE_REVIEW.md) | Core、Workshop、Harness 的调用与权威状态 |
| [SECURITY_REVIEW.md](SECURITY_REVIEW.md) | 原合并的授权、隔离、主体和提示词边界 |
| [DATABASE_CONCURRENCY_REVIEW.md](DATABASE_CONCURRENCY_REVIEW.md) | 迁移、锁、幂等、双库与物理恢复 |
| [HARNESS_MODULE_REVIEW.md](HARNESS_MODULE_REVIEW.md) | 七项模块能力的生产调用追踪及生命周期 |
| [CREATIVE_REVIEW.md](CREATIVE_REVIEW.md) | 九层判断、稿本来源、文学证据及三臂解读 |
| [PERFORMANCE_COST_REVIEW.md](PERFORMANCE_COST_REVIEW.md) | 有界运行、历史查询、费用及容量风险 |
| [TEST_COVERAGE_REVIEW.md](TEST_COVERAGE_REVIEW.md) | 真实入口、夹具、运行结果与未覆盖区域 |
| [FINDINGS.md](FINDINGS.md) | 九项问题的逐项定位、影响和最终状态 |
| [FINDINGS.json](FINDINGS.json) | 同一台账的可机读数据与成对提交 |
| [REPRODUCTION.md](REPRODUCTION.md) | 原树、修复树和历史数据的实际复现 |
| [REMEDIATION.md](REMEDIATION.md) | 修复提交、主线对应提交、延期决定和复核 |
| [MAIN_MERGE_RECEIPT.md](MAIN_MERGE_RECEIPT.md) | 首次合并、逐项挑选、主线复验与分支隔离 |
| [FINAL_VERDICT.md](FINAL_VERDICT.md) | 工程验收、部署边界与仍待人工确认的范围 |
