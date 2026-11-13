# MCP 协议、队列与负载实验的依据

核查日期为 2026-11-13。以下原型是可以检验的设计假设；性能结论以本分支真实运行的结果为准。

## 当前可用的协议

| 项目 | 已核实的行为 | 对本实验的影响 |
|---|---|---|
| MCP 2025-11-25 / stdio | Host 启动子进程，管道传递 JSON-RPC。客户端发送 `initialize` 并确认初始化。 | 在同一份合法绑定上复用已初始化客户端。分别计冷启动、热调用和重连，绑定数以同时握手成功且存活的子进程为准。[传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) |
| MCP 2025-11-25 / Streamable HTTP | 服务端初始化时可以颁发 `Mcp-Session-Id`。一旦颁发，客户端须在后续请求携带；会话失效按协议重新初始化。 | 进程内保有会话时需要正确的会话路由。Session ID 本身不能证明角色授权。NovelFactory 普通 HTTP API 的测试不覆盖此传输。[传输规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) |
| MCP 2026-07-28 | 请求逐项声明版本、客户端信息和能力，`server/discover` 可选。现代 HTTP 路径取消握手及会话 ID，并使用 `Mcp-Method`、`Mcp-Name` 支持入口路由。 | 通用协议层允许无会话副本；可信绑定仍须来自服务端，授权、幂等和预算仍须从持久业务层验证。对旧会话客户端另测。[版本协商](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning) · [发布说明](https://blog.modelcontextprotocol.io/posts/2026-07-28/) |
| 两代 Tasks | 2025 版是实验能力，包括 `tasks/result`。2026 版是独立的 `io.modelcontextprotocol/tasks` 扩展，以 `tasks/get` 轮询，支持 `tasks/update`。 | 两套线协议分别探测。返回领域任务 ID 应清楚标明为普通 NF 工具契约；查询、取消、读取结果都需校验当前权限与代际。[旧规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks) · [新扩展](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks) |
| 实际 Python SDK | 源实验 `pyproject.toml` 固定 `mcp==1.29.0`。该版本源码的 `LATEST_PROTOCOL_VERSION` 是 `2025-11-25`，可显式启用实验 Tasks；源实验 `make_server()` 注册 list/call，未启用 Tasks。官方 v2 同时支持两代核心协议，迁移文档及路线图均列出新版 Tasks 扩展尚未实现。 | 在 SDK 升级专项中测试新旧客户端/服务端互操作。不要给 1.29 客户端声明 2026 能力，也不要将 v2 类型存在视为扩展已经可用。[v1.29 类型](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/src/mcp/types.py) · [v1.29 实验接口](https://github.com/modelcontextprotocol/python-sdk/blob/v1.29.0/src/mcp/server/experimental/task_support.py) · [v2 迁移指南](https://py.sdk.modelcontextprotocol.io/migration/) · [SDK 路线图](https://github.com/modelcontextprotocol/python-sdk/blob/main/ROADMAP.md) |

## 候选与可证伪的假设

| 候选与来源 | 前提和最小原型 | 一致性风险及核心观察项 |
|---|---|---|
| A 原路径。MCP 1.29、Schroeder 等 NSDI 2006 | 同一绑定分别做固定并发、固定到达率。每次保留计划、发起和回包时间。先校验每个绑定确实访问自己的任务、上下文和工具。 | 闭环在慢响应时自动减少新请求。记录发送滞后、遗漏、拒绝、正文错误及客户端 CPU；把排队后成功与观察窗口内成功分别计数。[论文](https://www.usenix.org/conference/nsdi-06/open-versus-closed-cautionary-tale) · [wrk2 恒定到达率](https://github.com/giltene/wrk2) |
| B 绑定与连接预算。PostgreSQL 17、SQLAlchemy 2.0、gRPC | 为同时存活的绑定分配池预算；独立观察/维护/监督器连接留足空间。对同身份热连接与冷拉起分别计时。记录 checkout 时刻和 wait，线程结束之后才收还在途槽。 | PG 的 `max_connections` 是集群级上限并有保留槽。`pool_size` 是进程上界，SQLAlchemy 按需打开。连接不可跨进程继承使用。客户端取消并不会保证同步 handler 停止；不可提前归还连接或重发可能已提交的事务。[PG17](https://www.postgresql.org/docs/17/runtime-config-connection.html) · [QueuePool](https://docs.sqlalchemy.org/en/20/core/pooling.html) · [gRPC 取消](https://grpc.io/docs/guides/cancellation/) |
| C 授权读集合化。Psycopg 3 pipeline、Orca OSDI 2022、vLLM SOSP 2023 | 优先试单次调用内消除同一授权快照的重复读。跨调用仅在兼容积压时成批，给定批大小与最长等待，逐项绑定 Future、请求 ID、Principal、作品、版本与代际，数据库事务完成逐项授权。 | Pipeline 缩短等待往返，多条命令依然分别执行，一项失败影响同步点前的后续命令。集合 SQL 需分别验证同键不同身份、撤权、单项失败、事务错误。Orca 可借鉴的仅是动态聚合边界，vLLM 的 GPU/KV 分页和性能数字不能换算成 PG 收益。[Psycopg pipeline](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html) · [Orca](https://www.usenix.org/conference/osdi22/presentation/yu) · [vLLM](https://arxiv.org/abs/2309.06180) |
| D 限流与公平。Shreedhar / Varghese DRR、Demers 等 FQ、Temporal Workers、Netflix concurrency-limits | 等总预算下比较 FIFO、按作品轮转的加权 DRR、按虚拟完成时间选择的 WFQ、老化及固定/自适应槽；执行代价无法提前知道时将估计误差列入实验。租约/控制流留保障份额，空闲槽可借。 | 最繁忙作品有长期积压时检查其他作品首项/末项等待和续租过期数。Temporal 将排队和 Worker 实际槽分开；资源型策略以 CPU/内存调整，无法保证不超目标。限流器按响应延迟变化调节时须单独观察快拒绝与数据库拥堵，避免正反馈。[DRR 论文](https://web.stanford.edu/class/ee384x/EE384X/papers/DRR.pdf) · [FQ 论文](https://people.eecs.berkeley.edu/~kubitron/courses/cs262a-S16/handouts/papers/dks89.pdf) · [Temporal](https://docs.temporal.io/develop/worker-performance) · [Netflix](https://github.com/Netflix/concurrency-limits) |
| E 提交与查询。MCP Tasks、Temporal 执行槽 | 快速提交现有 TaskService 事务并返回领域 ID，Supervisor 发起工作，等待事件后再次领取。只在能力实际协商成功时研究原生 Tasks。 | 与同步入口核对相同的幂等键、代际、租约、尝试、预算及 Artifact。客户端断线后观察正式效果和账本。UNKNOWN 待持久化核对；不重发。模型等待中的 PG checkout 数和工作槽是两个指标。[Tasks 扩展](https://tasks.extensions.modelcontextprotocol.io/) · [Worker 槽](https://docs.temporal.io/develop/worker-performance) |
| F 工作分配。Ray Serve 2.60、Ray Actor | stdio Host 只向持有相应可信绑定的进程分配请求。多副本远程入口先满足协议/身份/会话约束，再在合格副本内比较轮询、最少在途、二选一和有界局部性。Ray 将 actor 资源和请求路由分别管理。 | 同一总 DB 连接和 Supervisor 槽预算比较；记录在途计数的新鲜度、偏斜、排队及排空。路由器只改变去向，不承担持久任务唯一领取。不在本实验引入框架。[Ray Serve 路由](https://docs.ray.io/en/latest/serve/llm/architecture/routing-policies.html) · [Actor](https://docs.ray.io/en/latest/ray-core/actors.html) · [自动扩缩](https://docs.ray.io/en/latest/serve/advanced-guides/advanced-autoscaling.html) |
| G 混合 | 只组合已单项通过的相容改动，保持相同 PG、线程、身份、业务分布和到达率，重跑 A/B/B/A。 | 关注队列等待叠加、批次造成的长尾、低负载税、长模型对短读的干扰。关注最差作品、控制流的尾延迟、排空和正确性；单项增益不可相乘。[长尾论文](https://research.google/pubs/the-tail-at-scale/) |

## 观测和证据边界

- 每个阶段各自记录计划到达、SDK 发起、MCP 入口收到、槽位获取、数据库 checkout、业务返回、客户端得到可验证结果。若阶段没有插桩，填 `null` 并标注缺失，不能用 `tools/call` 开始时间或 PG 连接数替代服务端准入计数。stdio 通过独立文件或 stderr 输出实验诊断，stdout 专供协议。
- 读查询也可能产生审计或登记状态。声明可重试前检查主体、任务代际、正式版本和所有可能的持久副作用。数据库侧 `pg_stat_statements.calls`、SQLAlchemy 游标事件、SQL 延迟、pool wait 和锁等待样本分别报告。没有观测到锁等待不能证明整个运行期间无等待。[PG 服务端统计](https://www.postgresql.org/docs/17/pgstatstatements.html) · [PG 活动与等待](https://www.postgresql.org/docs/17/monitoring-stats.html)
- 在近似稳态用 Little 定律按层核验平均在途数、有效到达率与平均停留时间是否一致。使用相同时间窗、单位、请求类别和边界。有限突发先给到达、排空、拒绝及悬挂的事件清单；P95/P99 和在途进程数均不能直接代入该公式。[Little 1961](https://doi.org/10.1287/opre.9.3.383)
