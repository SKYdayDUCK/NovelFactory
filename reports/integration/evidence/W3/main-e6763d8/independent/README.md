# W3 实际主线的独立工程复验

PR [#10](https://github.com/SKYdayDUCK/NovelFactory/pull/10) 的实际合并提交为 `e6763d844f023d7c6a13060dbaebca0416fe3099`，两服务在各自虚拟环境检出相同源树 `7edb5550e8cbe3c1feb70ff8baa0c79974cf8155`。第一父为 NF 桥接主线 `15f606c99c765deeffd904ad540441eb6476b3af`，第二父为通过资格测试的 W3 候选 `a15f41b40ed4a0cd4716e7335954ec0a1c5efadf`。[托管作业实际检出和 ZIP 校验](../workshop-ci.json)

| 实测范围 | 主线结果 |
|---|---|
| 原生 Workshop、W0–W3 历史数据升级、NF 工具、双服务、两侧 WAL 和桥接 | 10 组共 92 项通过，1 项 Docker 停机用例因本机缺少 Docker 跳过。GitHub 同一 main 的 Workshop 和 NF 桥接共 93 项通过、0 跳过，含该 Docker 用例。 |
| PostgreSQL 隔离 | AW 系统标识 `7707004783064248397`，NF 系统标识 `7707004788839940199`。两个 17.11 集群分别配置服务账户，历史 w002c、w003、w003a 库迁移到 `w003b`。 |
| Redis 8.0.2 全量丢失 | 两次初始空实例 run_id 分别为 `499ce0cd474895b713ea6b63a5cb12e2cc51f577`、`2489665b2d3561b3f9cdf7d85777cd2cec6e3c34`。AW outbox 事件与 3 个 NF 任务事件原标识一致。 |
| 丢回执、服务及 worker 替换 | 原委托 `562e0530-a63b-4030-8599-d652ff92ab0c` 的 AW 重启状态为 RECONCILING，原预留为 HELD（1.000000），新 worker 回到 BOUND。NF GET 返回原回执，同一 WorldRef、代际、任务和 operation 可查。 |
| 物理备份和双向恢复 | 两个原 WAL 落点 `0/40153E8`、`0/4063330` 均精确回放。过期对端的已结算历史保留为 WAITING_OPERATOR，明确的合成票据结算 0.020000 USD，保留 1 个 NF 任务。供应商调用 0。 |

[独立 JUnit、双向恢复、Redis 前后逐值及环境](local-qualification.zip) · [按套件统计、主线 SHA 和备份位置](replay-summary.json) · [托管 Workshop JUnit](../workshop-results.zip) · [托管 NF 桥接 JUnit](../nf-bridge-results.zip)

Linux 主机另外使用真实 bwrap、用户命名空间和冻结 Codex app-server 运行 17 项 Actor、Supervisor 和角色容器用例。每个 pytest 节点均可同 P1 托管适用的宿主限制比对。[原生角色隔离 JUnit](role-namespaces.zip)
