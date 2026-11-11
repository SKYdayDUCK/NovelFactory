# W3 单提交复演

NF 与 Workshop 都检出 `912d20b5f42cb39b0955e5015ac13cec182c75fe`，代码树为 `b49d1c054a78ae06bcb92354c094d6171707b9ea`。两个独立的 PostgreSQL 17.11 实例具有不同的系统标识，两侧各持有自己的数据库和最小权限服务角色。Redis 为 8.0.2，Python 为 3.12.14。10 组测试中 91 项通过，因环境没有 Docker 跳过 1 项容器停机用例。[原始回执、SQL 系统标识、命令和 10 组 JUnit](local-qualification.zip)

GitHub Actions 对同树合成提交 `6330f7e94ad5522348274be89674f399e6ff9d5f` 执行了原 Docker 用例，两份工件中的全部 92 项通过。Workshop 工件含 73 项，NF 桥接工件含 19 项。[作业及步骤](../workshop-ci.json) · [Workshop 原件](../workshop-results.zip) · [NF 桥接原件](../nf-bridge-results.zip)

历史库覆盖从已发布 w002c 和两种含旧委托与预留的 w003/w003a 数据库升级至 w003b。恢复对照涵盖已结算回执及新旧 WorldRef、Attempt 和结算不匹配；跨集群 stdio 复演验证授权、人员交接、预算竞争、撤权及丢回执。物理备份校验通过，本地两次目标和重放位置分别为 `0/40153E8`、`0/4063330`，最终保留 1 项任务，商业模型请求为 0。[逐套结果](replay-summary.json)

本目录记录 PR #10 公开候选的专项。合并后的正式主线需要另取实际提交并复验。独立 ZIP 的 SHA-256 为 `93d0cf16c59839bffc9f242b0667007c7592ce6a806060b299c0946875259aed`。

公开候选已继续前移到 `a15f41b40ed4a0cd4716e7335954ec0a1c5efadf`。这里保留上一版原始测量，新增组织预算下限和 Redis 全丢失后的 worker 恢复回执在后继版本独立验证。
