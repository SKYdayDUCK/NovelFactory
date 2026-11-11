# W3 公开候选的独立工程复演

NF 和 Workshop 均在 `a15f41b40ed4a0cd4716e7335954ec0a1c5efadf`、树 `7edb5550e8cbe3c1feb70ff8baa0c79974cf8155` 上运行。GitHub 拉取请求的合成提交 `84eceec0cdd5718457e6b1cd097d078e9702ae7f` 与候选同树，合成基线为正式 NF 桥接主线 `15f606c99c765deeffd904ad540441eb6476b3af`。[作业检出及工件摘要](../workshop-ci.json)

独立环境的 10 组用例共 92 项通过，1 项因环境没有 Docker 跳过。托管 Workshop 工作流运行了原容器停机用例，74 项 Workshop 和 19 项 NF 桥接用例全部通过。[独立运行的 JUnit、环境、过程记录和复现说明](local-qualification.zip) · [托管 Workshop ZIP](../workshop-results.zip) · [托管 NF 桥接 ZIP](../nf-bridge-results.zip)

- 两个 PostgreSQL 17.11 集群分别持有服务身份，系统标识在原件中可逐一核对。已发布的 w002c、含旧委托和预算预留的 w003/w003a 走各自迁移路径升至 w003b。授权、人员移交、撤权、预算并发与降额下限均在真实数据库中复核。
- 两个全新、初始为空的 Redis 8.0.2 进程有不同 run_id。重建后 AW 和 NF outbox 的原事件标识保持一致。AW 重启进入 RECONCILING，原预留仍为 HELD；新 worker 对原 operation 完成 BOUND，NF GET 返回原提交回执。原件包内 `w3-redis-recovery.json` 可逐字段比对。
- 双 MCP 回执丢失和双向物理 PITR 通过，两个恢复目标均精确回放，保留一项 NF 任务，商业供应商请求数为 0。[逐套统计及 WAL 位置](replay-summary.json)

独立 ZIP SHA-256 为 `9d0e338943da928369929c9d81820b30d74059b915eae7851f1f1ae7188d1acf`。此记录用于候选验收，合并后的主线需用其实际 SHA 单独复验。
