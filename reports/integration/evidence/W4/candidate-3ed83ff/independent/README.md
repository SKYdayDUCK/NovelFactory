# Workshop W4 公开候选的独立实测

Workshop 与 NovelFactory 均检出 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`，树为 `cb601579cceafd8cd33d35664a31f8d9dd38c879`。测试使用两套 PostgreSQL 17.11 集群与各自的低权限服务身份、真实 MCP stdio、独立 Redis 8.0.2 和固定 Codex。13 组共 123 项通过、1 项因本机没有 Docker 跳过；另有 17 项原生 Linux 角色隔离测试全部完成。[分组数字、操作和原件 SHA-256](local-summary.json) · [角色隔离 JUnit](role-namespaces.zip)

| 范围 | 结果 |
|---|---|
| 既有部署 | 全新库、已签 W3 的两份独立旧库以及入点 w004/w004a/w004b 均达到 w004c。原来各 31、34、34 张表的旧列行摘要保持；三笔原 NF 回执覆盖两侧安装。审核数据保留手工/本安装批准、外方升级、已经签署的外方结果和幂等记录。完整关系、列、索引、约束/FK 和视图与新库一致，既有签收视图 OID/ACL 不变。 |
| Skills 与独立审核 | 五种共享策略、七种独立审核模式、职员调任、冻结哈希、撤权后历史读取和跨安装隔离均通过。样本见 ZIP 内 w4-skills-stdio.json、w4-legacy-review.json 和 w4-route-upgrades/。 |
| 付费审核链 | 原 NF 任务、attempt、回执、报告及版本可逐项复核，原预留 0.10，单次 loopback 模拟账单结算 0.020000 USD、18 token。供应商商业请求 0。 |
| 原文覆盖 | 附加独立运行用 Python profiling 读取原服务实际返回的覆盖记录，测试逻辑、数据库与服务均照常执行。范围 [0, 25)，71 UTF-8 字节，发送原文 SHA-256 与 ArtifactRef 一致。NF 同一 report 两次提交引用相同 coverage，保留范围、受控引用、任务代际及 NF report。工程样本采用固定文本。 |
| 故障与恢复 | 原始 Redis 进程退出后从全新 Redis 实例重放 AW/NF 原事件，重启 AW 和更换 worker 后复得同一 operation。双向物理 PITR 的两个目标 `0/4020B10` 与 `0/406F0E8` 均精确回放，保留一个 NF 任务。 |

[13 组原始 JUnit、旧库数据与故障恢复](local-qualification.zip) · [同一附加审核实际覆盖、NF 报告、模拟费用及观测器](observed-coverage.zip) · [静态设计包](package-validation.json)
