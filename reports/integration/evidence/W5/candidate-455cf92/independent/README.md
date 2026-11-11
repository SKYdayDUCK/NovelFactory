# W5 公开候选独立实测

Workshop 与 NovelFactory 均检出 `455cf92cc6fe5e1d0e565d627a4d5651a56bc8d4`，Git 树 `d6cf8513c8df97902eb08fe1666492097d3d025a`。本轮 15 组原生测试共 144 项通过，1 项 Docker sidecar 用例在本地跳过。同一项以原生 Redis 进程启停复验通过，另有 17 项受限 Linux 角色测试通过。生产供应商请求为零。[原始分组和工件校验](local-summary.json) · [JUnit 与业务回执](local-qualification.zip)

| 实测范围 | 观测结果 |
|---|---|
| 安装与管理者恢复 | 实际 Workshop 作者 MCP 返回待办 `ab6c91f0-da27-41c3-aa7f-04730d037c03`，使用已安装的 `aw-manager-run --route` 调用另一个 PostgreSQL 集群上的 NF 项目，结果 `IDLE`。新 broker 再启动返回 IDLE，外部进程探针无触发，没有创建故事任务。 |
| Workshop 离线时的 NF | 物理钻演使用独立临时的 PostgreSQL 17.11 集群，两个库和两个服务身份。关闭 Workshop 库的新连接，验证无现存客户端，作者 stdio 的原唤醒操作返回 `VIEW_DENIED`。同一期间 NF 督导先存结果、再接受，真实 NF MCP 取回同一任务、回执和唯一 Attempt。Workshop 恢复后原待办保持 PENDING，随后按原键对账。该测试覆盖两次真实督导扫描。 |
| 物理恢复与账本 | 两个 WAL 命名目标 `0/4026790`、`0/407A3C0` 精确回放。迟到的受控发票结算 0.020000 USD，原始托管和 UNKNOWN 记录、冻结及解除结果见 paired.json。 |
| 已发布数据库 | 使用真实旧版 w004c、w005、w005a、w005b 建库，分别留存 34/34/37/37 张表的旧字段数据，历史 trigger 模式、原视图 OID/ACL、旧唤醒归属与任期均核验，四条路径的最终目录一致。原 w005、w005a 的无权唤醒撤销，w005b 合法旧唤醒只执行一次。 |
| 既有工程功能 | W0–W4 迁移、授权、Redis 更换、委托和付费审核均重跑。原文实际读取 [0,25)，共 71 个 UTF-8 字节，覆盖引用进入 NF 报告。独立审核以模拟单结算 0.020000 USD、18 token。[审核读回](observed-coverage.zip) |
| 通知停机与角色宿主 | 通知适配只替换 Docker 进程控制，保留原测试的数据库、真实两次 worker、PEL 与 ACK 检查。重启后单个收件记录，原始 Docker 节点的本地跳过另有完整 JUnit。[原生通知停机](native-notification-outage.zip) · [受限角色](role-namespaces.zip) · [工程包自检](package-validation.json) |

本次 [Workshop 正式工作流](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43110195132) 实际检出 `4de78767e03cc08c7940602737f08d559b170370`，Git 树与本地相同。按两个顶层工件逐份计算，共 145 项通过，跳过、失败与错误均为零。四份旧库数据和首次重启语义与独立运行一致。[托管逐项计数与离线/恢复](hosted-workshop-summary.json)。
