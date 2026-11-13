# P6 运行日志导出

在已登记的工作实例上使用项目控制绑定。每份 JSON Lines 文件记录一个作品、一个分支在同一 PostgreSQL 快照中的证据。原生灵感入口的筹备项目与正式作品有各自的权限边界，分别用以下命令归档。

```bash
python scripts/p6_runtime_evidence.py --binding "$PRIVATE_PROJECT/preparation-control.json" --output idea-runtime.jsonl
python scripts/p6_runtime_evidence.py --binding "$PRIVATE_PROJECT/control.json" --output book-runtime.jsonl
```

命令只读本地账本与审计记录。目标目录须已存在。文件以仅所有者可读写权限在新路径发布，已存在的文件保留。标准输出返回作品 ID、分支 ID、各记录类型的行数和整份文件的 SHA-256。相同数据库状态可以导出逐字节相同的文件。

| 记录 | 可复核的事实 |
| --- | --- |
| `scope`、`selection` | 作品、分支，以及该分支已选的 Profile、DAG、篇幅、滚动游标和盲评版本的不可变引用 |
| `task_generation` | 角色、任务、各代输入摘要、WorldRef 版本、角色及 Skill 版本、冻结运行时摘要、上下文引用和依赖摘要、工作进程领取与已登记等待的起止时间 |
| `task_transition` | 在业务事务中写入发件箱的状态变化，包含原始事件 ID、代际及时间。可在审计消费滞后时使用 |
| `call` | 模型调用 Attempt、预算、预留与实际费用/Token、供应商与模型标签、输入、Prompt、响应和候选的引用及摘要、输出状态、耗时与账本更新时间 |
| `event` | 原始 LogService 事件、因果链、工具调用、结果、错误、artifact 引用和耗时，可用任务或 Attempt 关联 |
| `business_receipt`、`world_commit` | 筹备项目中的灵感登记、初稿设计、独立事实核对，正式项目中已登记的正文、对账、接受、重试、续卷、重规划、盲评轮次及其回执。正式世界提交另带世界版本、父版本、manifest 与状态摘要 |
| `release` | 已冻结各卷的 RELEASE 与 manifest、内部发布回执和依阅读顺序排列的正文 ArtifactRef |

调用行的 `FIXTURE`、`PROVIDER`、`UNOBSERVED` 分别表示已识别的测试配置、报告了提供方配置的审计、以及尚无可匹配审计的账本行。判断某次真实调用的交付与结算时，应同时核对执行类型、候选状态、原始回执摘要、费用与账本状态。缺失的数值保留为 null。账本额度以该次调用对应的币种记录。供应商账单原件由有权人员单独对账。

人物认知、Prompt、正文、原始 Provider 响应、工具参数、原始端点和访问凭据不进入导出。需要核对原文时，持对应授权读取该条不可变引用。双盲评分的逐条引文、意见及其正文来源由受权评阅记录保存。

[PostgreSQL 17 导出回归回执](evidence/runtime-evidence-export.xml)覆盖已结算及待核对的调用、隔离和撤权、私有文件、重复导出和内部发布正文的顺序。

[跨平台私有文件发布用例](evidence/private-export-unit.xml)验证中断时清除临时文件、目标路径在成功前不可见、已有文件保持原内容。
