# R1 补充技术证据：时间、并发与负载

核查日期：2026-11-10（中国标准时间）。[r1_sources.json](r1_sources.json) 记录23组一手来源、团队实际阅读范围、项目推论和限制。原 [sources.json](sources.json) 与论文卡片继续保留；重复核查同一官方能力不计算成新增论文或新增实验。

## 1. 证据如何影响设计

| 来源组 | 核查范围 | 本项目采用与验证 |
|---|---|---|
| [R1-E01 SQLite WAL](https://sqlite.org/wal.html) | 读写并发与单writer限制 | 多worker权威接受仍以PostgreSQL做真实集成验收；SQLite仅辅助 |
| [R1-E02 PgBouncer配置](https://www.pgbouncer.org/config.html)、[E14功能表](https://www.pgbouncer.org/features.html) | 客户端/服务端池、transaction模式兼容 | 多进程总池预算；引入pooler前核查会话依赖 |
| [R1-E03 k6模型](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/open-vs-closed/)、[E04未发出迭代](https://grafana.com/docs/k6/latest/using-k6/scenarios/concepts/dropped-iterations/) | 闭环自限、独立到达和drop归因 | 同时设计行为与过载负载，保留计划/发出/完成/丢弃数据 |
| [R1-E05 事务重试](https://www.postgresql.org/docs/17/mvcc-serialization-failure-handling.html)、[E11隔离](https://www.postgresql.org/docs/17/transaction-iso.html)、[E12锁](https://www.postgresql.org/docs/17/explicit-locking.html) | 快照、完整事务重试、锁顺序 | 短事务复验；封闭依赖守卫；不把事务抢锁当世界裁决 |
| [R1-E06 SELECT](https://www.postgresql.org/docs/17/sql-select.html) | SKIP LOCKED队列用途与不完整视图 | 队列领取可以跳锁；世界完整查询不可以 |
| [R1-E07 Session](https://docs.sqlalchemy.org/en/20/orm/session_basics.html)、[E13池与fork](https://docs.sqlalchemy.org/en/20/core/pooling.html) | 并发Session边界与子进程连接管理 | 每任务短UoW，子进程正确初始化池，模型等待释放连接 |
| [R1-E08 SQL统计](https://www.postgresql.org/docs/17/pgstatstatements.html) | 聚合SQL执行/块I/O/WAL | 定位瓶颈；请求尾延迟另从trace测量 |
| [R1-E09多列索引](https://www.postgresql.org/docs/17/indexes-multicolumn.html)、[E10范围](https://www.postgresql.org/docs/17/rangetypes.html)、[E17部分索引](https://www.postgresql.org/docs/17/indexes-partial.html)、[E23类型](https://www.postgresql.org/docs/17/indexes-types.html) | 查询谓词、范围和访问路径 | 时态/实体历史先在关系数据库实现并检查实际计划，不预定性能收益 |
| [R1-E19日期时间](https://www.postgresql.org/docs/17/datatype-datetime.html)、[E20递归](https://www.postgresql.org/docs/17/queries-with.html)、[E21 btree_gist](https://www.postgresql.org/docs/17/btree-gist.html)、[E22约束](https://www.postgresql.org/docs/17/ddl-constraints.html) | 类型、递归/环、组合范围约束与跨行限制 | 虚构历法显式映射；图遍历有预算；跨行领域不变量受控验证 |
| [R1-E15 MCP基本规则](https://modelcontextprotocol.io/specification/2026-07-28/basic)、[E16传输](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)、[E18变更记录](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx) | 2026 profile的无协议会话、请求元数据与取消 | 连接不承担业务任务身份；旧/新适配分别验证，不能混拼 |

官方资料支持组件行为和限制。scope足迹完整性、跨时间因果覆盖、同一批准在新parent上继续适用、角色知识裁剪和小说质量，是本项目自己必须验证的机制，不由这些文档自动证明。

## 2. 研究转研发的新增任务

- **P1 确定性边界：** 类型化统一查询、scope注册与所有writer更新、负向范围、guarded证书事务内复验、批量固定切面、无状态入口及总连接预算。
- **P2 实际闭环：** A–H组合案例、50/200行为负载、两线交汇、计划失效后的局部重规划、真实Host与完整短卷。
- **P3 有证据再扩展：** 500/1000探索档位、复杂scope粒度、共享缓存、队列和图查询候选比较。

这些任务已进入 [11](../docs/11_研发路线与测试计划.md) 和 [15](../docs/15_并发压测与端到端验收.md)，不是另起一套系统。

## 3. 实际执行与未执行

实际执行的是标准库 [calculate_load.py](../benchmarks/calculate_load.py)，固定输入产生5档×3缓存假设的15行流量需求，另有虚拟到达计数、闭环敏感性和池预算算术。结果状态明确为 `MODEL_CALCULATED_NOT_SERVICE_BENCHMARK`。

未执行：PostgreSQL负载、网络吞吐、Host兼容、付费模型并发、A–H业务流程、长时运行与任何论文复现。因此数据库RPS、响应P95、可承载Agent数和费用均没有本轮实测值。技术版本在正式实现前仍需锁定到具体可复现环境。
