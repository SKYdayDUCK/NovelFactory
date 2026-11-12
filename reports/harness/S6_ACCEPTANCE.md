# S6 工程验收

NovelFactory 负责世界状态、任务代际、正式接受和费用账本。Workshop 负责组织身份、Skill 授权、撤销和组织端回执。S6 在这两套记录中关联作品、分支、任务、代际和调用。操作员通过 `nf_harness_metrics` 在授权分支上读取有界的时间窗口。[生产日志字段](S6_OBSERVABILITY.md) · [指标及分母](S6_METRICS.md)

| 验收范围 | 执行入口和核验依据 |
| --- | --- |
| Python、静态角色、历史工作流 | `contracts` 与 `history` 留存逐案回执。历史组涵盖 Codex 进程恢复、编辑交接和真实 Redis Streams。 |
| 身份、私有认知、材料和任务代际 | `harness` 通过生产网关及角色 MCP 读取冻结的 ContextPacket，核对不同演员的 BELIEF、受限来源、关闭后恢复及迟到交付。 |
| 计划、模拟、细审、返工 | `harness` 运行规划、模拟、编辑决定及待修改稿，再通过独立复审确认。正式正文与世界状态沿用 Core 的接受流程。 |
| 动态角色与 Skill、候选评估及发布 | `dual` 在独立 PostgreSQL 实例启动 NF 和 Workshop，通过受限服务身份、版本切换和撤权前后的真实分派核实。S5 的发布、回滚和后续任务纳入 `harness`。 |
| 窗口指标、场景消耗、观测权限 | 操作员 stdio 端口查询有界汇总。测试用角色和其他分支核对权限，并区分模型 Attempt、实际用量、未知金额与没有分母的比率。 |
| 并发、网络错误、Redis 丢失、重复请求 | `faults` 覆盖并发任务、发送后未知、模型失败、幂等及恢复。`paired-restore` 恢复两个系统的独立备份，核对受理、回执、重投和原预留。 |
| 物理 WAL/PITR | `pitr` 验证 PostgreSQL 基础备份并回放到命名恢复点。恢复后重算基础备份之后写入的上下文摘要、核对本地阻断回执、重查角色权限、排除目标点后的哨兵，并核对未决费用、代际和 Redis 进度。 |
| 旧库、P5.6、P6 | `upgrades`、`workshops-upgrades`、`p6` 使用有数据的旧版本。NF 0019→0020 与 Workshop w010→w011 另在 `harness` 和 `dual` 验证。 |
| 三组离线工程对照 | `comparison` 先冻结场景窗口与配置，再运行静态、固定 Harness、动态配置及候选优化三种完整流程。每组保存调用、结算及具体位置的判定。[三组实验与历次诊断](S6_ENGINEERING_COMPARISON.md) |

## 运行和核对

使用仓库锁定的 Python 3.12 依赖，另在运行独立子服务的同一环境安装检出的 Workshop 包。调用 `python validation/run_p57_acceptance.py SUITE --output PATH`，其中 PATH 是本次使用的新目录。运行器记录检出 SHA、工作目录差异、命令、时长与退出码。双服务使用不同 PostgreSQL 17 实例；测试为两个服务分别创建受限身份并验证不能交叉登录。历史升级用例还需要原 Agent Workshop 工作流中预置的各套旧库。[逐组签收](evidence/s6/receipt.json) · [物理恢复](evidence/s6/physical-pitr.json) · [双服务恢复](evidence/s6/paired-restore.json)

S6 开发期间工作区中断。独立运行器在 S5 上重放已保存的改动，生成的 30 个产品文件及整树哈希与断开前完全相同。新运行器首次启动双服务时缺少已安装的 Workshop 包，随后补齐 Workshop 和双服务重启所需的本机 Redis 可执行文件，并在同一产品源码上重跑。96 项历史用例中有 17 项要求原生 Linux 命名空间；完整复验使用支持该机制的独立运行器，临时数据库创建者有权在测试结束后清理测试连接。NF 与 Workshop 服务继续使用各自受限的登录身份。一次共享主机上的补充 HTTP 测试得到 293/300；同一冻结用例在独占托管运行器得到 300/300，连接池上限为 8，429 为 0。[冻结树回执](evidence/s6/restored-product.json) · [原始和重复测量](evidence/s6/supplementary-http.json)

工程实验的商业请求数为零。P6 真实本地商业创作保持 `BLOCKED_LOCAL_AUTHORIZATION`，独立文学评估保持 `NOT_EVALUATED`。Issue #13 的 AT-30 原门槛和 `NOT_MET` 结论保留。
