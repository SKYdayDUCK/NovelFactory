# NovelFactory 数据库执行优化实验设计与验收

## 结论与采用范围

唯一通过最终真实接口双向对照的改动，是在任务详情 GET 的短事务里合并任务、同代输入和所属作品的读取，再独立锁定并核验当前授权。建议作为默认关闭的可选项交付。16 个闭环客户端、两部作品、上限 4 条应用连接时，纯 GET 的有效吞吐提高 14.4%–16.1%，SQL 执行减少 40%；加入 10% 独立续租，整体吞吐提高 14.4%–17.4%，两个方向的续租 P95/P99 都符合预登记上限。完整时间线 HTTP 的历史门槛仍未达到。全局调度、模型预算、世界提交和运行时默认配置继续使用冻结路径。[最终接口实验](../../reports/performance/p59_database/raw/actions/43294749933/) · [历史门槛](../../reports/performance/p59_database/HTTP.md)

## 范围与证据

实验分支为 `experiment/p59-database-throughput`。原始对照取自正式 main `434840593179e6c1faa6b7aeca3d7d747644506d` 和 PR #33 冻结提交 `3e6b59268f1e89520230c8b70e9a26b9950a2f79`。每轮性能对照固定应用连接预算、负载、事务和验收口径。各组在独立 runner 上运行，同一组正序、逆序串行，每轮重新建立数据库和对象目录。主线旧审计目录在发布树中已精简，研究时从 PR #32 的完整审计提交 `5a2ddaaf97ea5c5312edeef55240bd9186326db1` 读取原始材料。[预登记门槛](../../reports/performance/p59_database/PROTOCOL.md) · [冻结运行时](../../reports/performance/p59_runtime/METHOD.md) · [旧优化审计](https://github.com/SKYdayDUCK/NovelFactory/tree/5a2ddaaf97ea5c5312edeef55240bd9186326db1/reports/performance/p59_optimization)

## 当前调用路径

HTTP 请求经过 ASGI 接入、Bearer 凭据校验、每身份速率限制和业务准入，再进入应用服务。完整时间线查询固定作品、分支、版本、角色和知识切面，在 PostgreSQL 中抽样、生成应答并在交付前再检查授权、任务代际、期限和租约，登记语义依赖。一次逻辑“读”也可能写观察记录。MCP stdio 与进程内绑定工具经过共同的身份、工具可见性、参数校验和审计逻辑。HTTP 的任务详情接口和 MCP 的绑定任务查询有各自的业务契约与授权路径。[HTTP](../../src/novel_factory/interfaces/http.py) · [时间线服务](../../src/novel_factory/application/queries.py) · [绑定工具](../../src/novel_factory/application/bound_tools.py)

`Database.uow` 为一次业务工作提供独立连接和事务。写事务执行原变更门禁，固定世界版本抽样可使用可重复读。历史基线已有批量插入、Scope 的有序去重 upsert、固定主键的任务领取 SQL、连接池上限、跨作品公平调度、租约及 Attempt 账本。本实验保留这些机制。作品共享锁之后获取授权共享锁，撤权和策略修改仍在权威行上竞争。世界接受、Scope/HEAD 核验、预算扣减、Attempt 发送/结算和执行租约各自保留完整的事务边界。[UoW](../../src/novel_factory/infrastructure/database.py) · [仓储](../../src/novel_factory/infrastructure/repository.py) · [任务及租约](../../src/novel_factory/orchestration/tasks.py) · [调度器](../../src/novel_factory/orchestration/dispatch.py)

## HTTP 容量及根因

下表使用同样的 1,000 次/秒计划到达率和 5,000 次独立请求。每个分层 runner 在自己的机器上先校准，两次四分片纯传输均完成 5,000/5,000。新发生器先序列化请求，子进程使用独立事件循环和长连接，校验进程按每个身份的期望结果逐项核对。HTTP 头写入时刻在客户端观察，服务端收件与实际进入业务槽有独立计数。早期数据中 admission 的 admitted 统计授票，超时竞态可能发生在授票与真正进入之间。后续对照直接在接入和业务槽入口计数。[分层证据、计时及复现](../../reports/performance/p59_database/HTTP.md)

| 同机单层测量 | 有效/5,000 | 429 | 有效响应/秒 |
|---|---:|---:|---:|
| 生产健康检查 | 5,000 | 0 | 992.30 |
| 认证和最小受权逻辑 | 2,472 | 2,528 | 481.52 |
| 认证及本人任务查询 | 2,447 | 2,553 | 295.34 |
| 完整固定版本时间线 | 93 | 4,907 | 8.88 |

原 HTTPX 发生器在这些 runner 上对固定 2,104 字节响应的头发送率为 208–254 次/秒；单分片轻量发生器也接近 1,000 次/秒，因此不能把提速全部归于多进程。完整业务过载时，客户端调度延迟 P95 为 1.57 ms，但等可用连接的 P95 约 2.69 s，只有 3,162 项在计划的 5 秒内开始发头。服务端最终收到 5,000 项；认证拒绝 4,459 项、认证等待超时 356 项，业务拒绝 8 项、业务等待超时 84 项。池的两个进程峰值之和为 8。这组计数能够定位拒绝发生的阶段；它不能单独判定每一项等待由哪条 SQL 或 Python GIL 引起。各层的有效速率不能相减来推算单段服务时间。[逐层原始记录](../../reports/performance/p59_database/raw/http/verified-results.json)

## 业务、协议与数据库

| 测量对象 | 负载、校验及观察 |
|---|---|
| 进程内 Agent | 两个独立绑定各执行 60 次完整工具调用。两个角色各获 60/60 个正确结果，约 38.99 和 38.79 项/秒，各 2,100 次 SQL 执行，包括审计。 |
| MCP stdio | 两个真实子进程和独立绑定，闭环 120/120，73.21 项/秒。每秒 12 项的开环 120/120，12.07 项/秒。角色、作品与内容逐项核对。 |
| 原始 SQL | 单独连接和临时表。64 项读取时逐条、pipeline、集合 SQL 两次中位用时 17.72、5.47、0.78 ms；64 项冲突写为 12.43、5.44、0.88 ms。约束冲突时显式核对整笔事务回滚。这组插入、更新和冲突是数据库微基准。 |
| 调度、领取与唤醒 | 五个独立数据库，各有两部作品，实际创建身份、任务、上下文、授权和队列，用受控模型适配器产生可核验回执。验证生成代际、领取次数、结果、唤醒、UNKNOWN 与取消、预算槽释放及两部作品 HEAD。 |
| 数据库繁忙时的调度 | 同一 50 工作流试件以 0/4/4/0 个真实背景读者串行复测。四个读者分别合计完成 7,788、7,873 次状态查询；应用连接峰值从 4 到 8。四轮业务验收均通过；竞争下总流程吞吐降到 2.30、2.32 个核验结果/秒，空闲对照为 6.22、6.78。 |

SQL 计数来自 SQLAlchemy 执行事件。带 `pg_stat_statements` 的第二轮另取数据库侧语句数、执行时间、缓存块和 WAL 差值。网络数据包、cursor 执行、事务控制语句、checkout 预检和业务请求有不同分母。读锁等待按采样记录，样本中没有等待不代表整个区间没有等待。独立 [EXPLAIN 计划](../../reports/performance/p59_database/raw/status-plans.json.gz) 在有 200 个读身份的库中捕获真实 SQL；单项合并输入采用 Nested Loop，8/32/128 项使用 Hash Join，作品和授权的 LockRows 均保留。[协议与绑定工具原始数据](../../reports/performance/p59_database/raw/actions/43290275128/) · [繁忙工作流与 PG 统计](../../reports/performance/p59_database/raw/actions/43291814205/)

| 持久化工作流身份 | 核验成功 | UNKNOWN / 发送前取消 | 从依赖等待唤醒 | 活动模型调用峰值 | DB 连接峰值/预算 |
|---:|---:|---:|---:|---:|---:|
| 50 | 49 | 1 / 0 | 9 | 2 | 4/8 |
| 200 | 196 | 2 / 2 | 39 | 1 | 4/8 |
| 500 | 490 | 5 / 5 | 99 | 2 | 4/8 |
| 1,000 | 980 | 10 / 10 | 199 | 2 | 4/8 |
| 2,000 | 1,960 | 20 / 20 | 399 | 2 | 4/8 |

五档均达到既定业务验收，商业模型调用为零。这些数字描述持久化工作流人口、两个执行槽及单机资源上界。每档使用独立 runner，跨行吞吐不用于推算系统随规模增长的加速比。[各任务原始记录](../../reports/performance/p59_database/raw/actions/43288881002/)

## 候选实现与事务语义

实验协调器接受两种内部类型。对 `TaskStatus` 逐项校验任务标识、服务端 Principal、当前作品/分支、授权、拥有者和任务当代输入。同一批按固定主键次序先锁作品再锁授权，按提交顺序将值或安全的领域错误交还给对应 Future。两个用户请求相同任务也要各自通过授权。状态接口返回该任务绑定的 WorldRef。世界时间线、角色上下文和未来计划继续走各自的独立业务校验。对可信监督器提交的 `LeaseRenewal` 逐项执行现有强事务。协调器没有接受任意 SQL 的接口。[类型化操作](../../experiments/db_execution/operations.py) · [协调器](../../experiments/db_execution/coordinator.py)

默认实验上限为 4 个执行槽、4 个数据库连接、128 个排队项及一个观测连接。固定 FIFO 微批只合并相邻兼容读，遇到租约便截断批次。分队列模式将读和续租轮转，同一个池执行。稀疏和饱和时实际填充率不同。自适应变体在空闲时把单项读直送原服务，繁忙时用观测服务耗时、最早请求的队列年龄、可用时间和兼容积压决定是否等待。样本未放行的改动均保留作显式实验。[候选开关](../../reports/performance/p59_database/CANDIDATES.md) · [初轮消融](../../reports/performance/p59_database/CANDIDATE_REVIEW.md) · [独立复验](../../reports/performance/p59_database/CANDIDATE_CONFIRMATION.md)

取消或到期的等待者在执行前丢弃；已派发的写操作不得因等待者离开而重发。数据库短暂失联的只读请求返回可重试 503，队列拥塞返回 429；在途写的未知结果需要查询持久租约或 Attempt 账本。队列仅在内存保存短期读需求，持久任务、租约和预算均由 PostgreSQL 恢复。调度器不持有跨请求的活动事务、角色数据响应缓存或模型连接。[数据库急停、恢复与 PITR](../../reports/performance/p59_database/raw/validation/README.md)

## 相关技术的取舍

| 方法 | 对本系统的适用范围与实测判断 |
|---|---|
| 事务内集合 SQL | 能同时返回多个独立键的受控操作，或同一强事务内部的多行变更。减少 cursor 执行和往返；须保证同键重复、行顺序、冲突和失败的原语义。对照使用真实 PG17 的返回行和回滚结果。 |
| Psycopg pipeline | 同一连接上的独立命令可减少等待往返。一个错误可使后续操作一直失败到同步点，因此仅在同一原子边界做原型。单连接微基准上仍慢于集合 SQL，保留工具，不引入批量世界提交。 |
| 连接池及多队列 | 共享固定连接预算，按业务类型测真实完成率和续租尾延迟。单纯加队列有时延后了其他类别，也可能增加已受理积压。连接失效可回收池，已开始事务的结果仍需按业务语义处理。 |
| 有截止时间的微批 | 大批读有可复现的数据库层吞吐收益，低并发下要计入排队和分发成本。对任务状态 GET 做了真实接口二次验证。 |
| 以活数据哈希减少固定版本传输 | 同一进程只保留一个按作品/分支/版本绑定的已验证快照文本。每次用 PG 的 SHA-256 复核当前字节，返回前走原校验并解码成独立对象。缓存和腐败注入反例均单独验证。其容量与尾延迟结论以 A/B/B/A 的完整查询结果为准。 |

资料与实验范围对应。PostgreSQL 的行锁与执行计划用于检验锁顺序和集合查询，服务端执行计划不含网络传输；驱动 pipeline 与数据库连接池文档解释故障边界。Clipper 的延迟约束批处理提供了设计参照，其模型服务性能数字没有计入本试验。[PostgreSQL 行锁](https://www.postgresql.org/docs/17/explicit-locking.html) · [EXPLAIN](https://www.postgresql.org/docs/17/using-explain.html) · [PostgreSQL 统计](https://www.postgresql.org/docs/17/pgstatstatements.html) · [字节哈希](https://www.postgresql.org/docs/17/functions-binarystring.html) · [Psycopg Pipeline](https://www.psycopg.org/psycopg3/docs/advanced/pipeline.html) · [SQLAlchemy 连接池](https://docs.sqlalchemy.org/en/20/core/pooling.html) · [Clipper](https://www.usenix.org/conference/nsdi17/technical-sessions/presentation/crankshaw)

## 方案筛选

初轮和预注册复验每次都保留完整请求。一次数据库层验证使用 50 个授权身份、两本书、四个执行槽、四条连接、1,000 项操作和 64 个闭环在途客户端。其中 900 项读取本人任务，100 项对两笔可信租约分别续期。下一表取修正读者轮转后的独立复验，两个数字分别为正序、反序。原始版本中 50 个已配置读者有 45 个实际读过状态，该轮另行归档。[确认轮原始与指标](../../reports/performance/p59_database/CANDIDATE_CONFIRMATION.md)

| 候选 | 已验证收益 | 决策边界 |
|---|---|---|
| 单一原路径 | 混合业务约 303/309 项每秒。所有读写匹配持久结果。 | 继续作为默认和回退路径。 |
| 仅分队列 | 初轮混合读写短续租部分受益，只读时出现更长 P95/P99，SQL 数不变。 | 无全局采用依据。 |
| 任务和同代输入 JOIN | 初轮真实 GET 两方向均提升。复验 130.21→146.69 和 133.13→143.14 项每秒，1,600→1,280 条 SQL；第二方向吞吐 +7.52%。 | 保留为简单对照，单凭该 JOIN 的四次 HTTP 样本未全部越过预登记门槛。 |
| FIFO 状态微批 | 混合数据库业务 1,048/1,046 项每秒，业务 SQL 从 4,600 降至 1,400。真实 GET 只有 125.47/122.16 项每秒。 | 在已有大量兼容的授权状态读积压时有实测价值；不作为所有入口的默认调度器。 |
| 微批与分队列、混合 JOIN | 部分饱和业务获得更高总吞吐，但混合 JOIN 复验 P99 约 298/317 ms，原路径为 233/224 ms。 | 续租和整体尾延迟挡住全局采用。 |
| 自适应 FIFO | 16,000/16,000 项通过。单并发每轮 900 个读均走原路径；8 并发实际触发反馈等待且吞吐下降。64 并发的数字改善时，两方案均形成 100 个九项读批、100 笔独立续租，SQL 均为 1,400 条。 | 保留实验开关。观察到的差异不足以证明自适应控制带来稳定收益。[完整消融](../../reports/performance/p59_database/ADAPTIVE_REVIEW.md) |
| 固定版本的 PG 字节校验 | 5,000 请求同机 A/B/B/A，基线 13.60/13.25、候选 15.55/15.10 次有效查询每秒。 | 拒绝。候选的计划到完成 P95 中位增幅 50.2%，P99 增幅 31.0%，均超过 10% 限额。[原始对照](../../reports/performance/p59_database/raw/actions/43291814145/) |

微批扫描覆盖 6 个批大小和 5 个等待窗口。首轮发现 `hybrid` 在上限为 1 时误走四语句原路径，修正后二次扫描的五格全部 480/480、每格 1,776 条应用 SQL。读业务之间按照任务 ID 和返回 Future 关联，同一批的拒绝只影响对应项。数据库整笔执行失败则所有依赖该结果的读项得到不可用；该批次没有持久化的成功声明。相同代际的写入幂等和 UNKNOWN 的账本处理仍由原业务实现。[参数及原始数据](../../reports/performance/p59_database/raw/candidates/confirmation.csv)

## 复现

使用 Python 3.12、锁定依赖、原生 PostgreSQL 17 和全新的实验工作树。候选程序会在指定的测试集群创建并销毁各自随机命名的数据库，库表和对象目录互不复用。以下命令从仓库根目录执行。历史压力测试包含按原口径返回非零的结果，保留退出码及 JSON。各 GitHub 作业完整记录 runner、命令、输入 SHA 与原始输出。

```bash
uv sync --locked --group dev --python 3.12
export PATH="$PWD/.venv/bin:$PATH"
out=$(mktemp -d)
python scripts/with_postgres.py --log-dir "$out/pg-mixed" -- \
  python -m benchmarks.p59_db_matrix --profile mixed --directory "$out/mixed"
python scripts/with_postgres.py --log-dir "$out/pg-grid" -- \
  python -m benchmarks.p59_db_matrix --profile grid --directory "$out/grid"
python scripts/with_postgres.py --log-dir "$out/pg-adaptive" -- \
  python -m benchmarks.p59_db_matrix --profile adaptive --directory "$out/adaptive"
python scripts/with_postgres.py --log-dir "$out/pg-plans" -- \
  python -m benchmarks.p59_status_plans --agents 200 --output "$out/plans.json"
python -m benchmarks.p59_http_shards --layer transport --workers 4 --output "$out/transport.json"
python scripts/with_postgres.py --log-dir "$out/pg-query" -- \
  python -m benchmarks.p59_http_shards --layer query --workers 4 --output "$out/query.json"
python scripts/with_postgres.py --log-dir "$out/pg-open" -- \
  python -m benchmarks.p3_open_arrival --agents 1000 --rounds 5 --arrival-rps 1000 \
  --hot-fraction .8 --api-processes 2 --output "$out/frozen-open.json"
python scripts/with_postgres.py --log-dir "$out/pg-workflow" -- \
  python -m benchmarks.p59_workflow_load --agents 2000 --output "$out/workflow.json"
python scripts/with_postgres.py --log-dir "$out/pg-hour" -- \
  python -m benchmarks.p3_stability --agents 500 --seconds 3600 --read-rps 20 \
  --api-processes 2 --renew-worker-leases --output "$out/renewed-hour.json"
```

原始一小时对照使用最后一条命令并去掉 `--renew-worker-leases`。精确复现数据库侧统计时，应使用启动时已预载 `pg_stat_statements` 的专用集群，并在其管理数据库安装扩展。SQLAlchemy 执行计数可以在没有这个扩展时独立运行。Linux 是负载测量平台；Windows 的发布资格由真实 PG、授权、租约及原生资源监测测试单独记录。[最终 PG 统计配置](../../.github/workflows/p59-db-confirm.yml) · [原 HTTP 门槛与一小时对照](../../.github/workflows/p59-db-capacity.yml) · [平台与停机恢复](../../.github/workflows/p59-db-platform.yml)

## 单项任务详情的后续实验与接入边界

二次复验发现 JOIN 的一条方向没有达到预登记的 10% 吞吐增幅。后续实验把作品锁与任务及同代输入的读取合并，保留独立的授权查询。首个原型在同一 runner 的 320 GET 正反两方向分别从 159.86 提升至 207.40、从 168.01 提升至 209.68 次/秒；每轮 SQL 从 1,600 降至 960，P95、P99 均下降。在内部 90% 状态读、10% 独立续租的基准上，第二方向从 397.92 降至 353.04 项/秒，P95 从 175.16 升至 456.07 ms。该轮语句数下降且采样未见行锁等待，现有证据无法把这次长尾精确归因于某个服务端锁。基于这个反例，最终实现将开关限定在 HTTP 的任务详情路由。[原始单项查询试验](../../reports/performance/p59_database/raw/actions/43293435711/)

1. HTTP 仍解析并验证 task UUID，使用原认证、速率限制与调度配额。只有 `GET /api/v1/tasks/{task_id}` 可选择新的查询方法。
2. 单个短事务内，按调用者提供的 task ID 读取任务，按数据库返回的 task ID 与 generation 双键连接其不可变输入。相关子查询以任务行的 work ID 读取并 SHARE 锁定完整作品行。works 读取使用 LATERAL 保证顺序；来源未知的作品或版本不能由请求者指定。
3. 第二条业务 SQL 读取并 SHARE 锁定 Principal 的授权行，再运行与默认路径相同的实时能力、分支范围、角色、拥有者和任务绑定检查。临时 resume 授权沿用单独的过期时间和策略版本核查。对常规有效身份，一次 GET 含独立认证从五条 SQL 降至三条；临时授权所需的额外 SQL 单独保留。
4. 返回前从任务与作品的分列结果生成原公开结构。不会把授权行、作品内部配置或其他任务状态交给申请者。事务在一次请求中结束。多个 HTTP GET 不共用事务、结果或申请者 ID，无需额外的定时器、数据库连接池或外部服务。
5. 工厂启动时读取 `NF_HTTP_FENCED_TASK_STATUS`，值为 `1` 才开启；显式构造应用可设置 `HttpSettings(fenced_task_status=True)`。关闭时设为 `0` 或移除变量并重启该 API 实例。普通 `Kernel.get_task`、MCP、进程内工具、模型计费、其他 HTTP 路由均沿用原流程。无数据库迁移。实验态的直接调用、协调器和混合写对照有单独的显式开关。

接入相关的源码集中在 HTTP 适配器、Kernel 和 PostgreSQL 仓储；针对读授权与错配绑定的测试、HTTP 工具和历史容量门槛可以独立运行。未来集成时同步移植这三个模块相应的差异及真实 HTTP 回归；原始实验分支以 PR #33 冻结提交为父，直接向 main 合并整条实验分支会携带其他 P5.9 工作。[HTTP 路由与启动参数](../../src/novel_factory/interfaces/http.py) · [应用层校验](../../src/novel_factory/application/kernel.py) · [仓储查询](../../src/novel_factory/infrastructure/repository.py) · [GET 与撤权竞态用例](../../tests/integration/test_p59_fenced_status.py)

### 定稿的真实 HTTP 对照

应用定稿源为 `6b8fd74d3ed8830a65b4f183bd58f9023cb4923c`。两组独立 Linux runner 各执行 A/B/B/A，A 关闭开关，B 打开。每轮 16 个闭环客户端、320 项操作、两部作品、16 个授予 writer 的身份。每次都走 Uvicorn TCP、真实认证与 PostgreSQL，成功须与原接口的完整 JSON 相符。混合组另外检查每笔续租的代际、期限、持有人和两部作品最终数据库记录。四轮的两个方向分别以下列配对比较，时间单位为毫秒。[逐请求记录与锁测试](../../reports/performance/p59_database/raw/actions/43294749933/) · [比较程序](../../benchmarks/p59_task_http.py)

| 负载与配对 | 有效项/秒，A → B | P50，A → B | P95，A → B | P99，A → B | 应用 SQL，A → B |
|---|---:|---:|---:|---:|---:|
| 纯 GET，正序 | 127.17 → 147.60 | 119.50 → 104.57 | 161.97 → 130.59 | 179.38 → 159.60 | 1,600 → 960 |
| 纯 GET，反序 | 130.54 → 149.34 | 119.11 → 103.09 | 146.44 → 130.93 | 172.86 → 147.16 | 1,600 → 960 |
| 288 GET + 32 续租，正序 | 204.80 → 234.31 | 73.33 → 65.54 | 134.21 → 91.12 | 147.50 → 103.89 | 1,760 → 1,184 |
| 288 GET + 32 续租，反序 | 198.80 → 233.46 | 77.09 → 64.83 | 117.15 → 93.67 | 153.80 → 112.59 | 1,760 → 1,184 |

共 2,560/2,560 项通过，连接峰值各为 4，结束时全部归还。混合组续租正序 P95/P99 从 38.35/40.81 到 36.62/42.14，反序从 36.78/44.99 到 37.89/39.88；每轮每部作品各续租 16 次。每轮续租 P99 取到该轮最大值，尾部证据只覆盖这个样本量。纯 GET 两对试验的客户端加服务端进程 CPU 从 2.88/2.81 CPU 秒降至 2.50/2.47，混合两对从 1.62/1.73 降至 1.42/1.39。该指标不包含独立 PG 进程。两种负载都满足已登记的吞吐、SQL、资源、错误与尾延迟条件。没有据此推算 1,000 客户端时的有效 GET 数或其他接口容量。

### 后续集成与回退

| 可移植单元 | 依赖与操作 | 必须核对的验收结果 |
|---|---|---|
| 任务详情 SQL 与权限验证 | 从源 `6b8fd74d` 取仓储的 `_task_with_locked_work_query`、`task_with_locked_work`，Kernel 的 `get_task_with_locked_work` 及其公共授权和返回值辅助方法。保留原模块已有实现，按符号移植。 | 撤权先/后获取锁、两个作品、过期临时授权、当前代际、取消与 UNKNOWN、受权结果全文；在目标 PG 上重测查询计划。 |
| HTTP 开关 | 同步移植 `HttpSettings.fenced_task_status`、构造应用时的环境读取与任务 GET 分派。采用默认值 false，按单个实例设置并重启。 | 普通 GET 与 90/10 混合 A/B/B/A，零意外失败，固定池上限，复核总量及续租 P95、P99。出现回归即移除环境变量或设为 0 并重启。 |
| 客户端、分层仪表和证据处理 | 可按文件独立引入 `benchmarks/p59_http_shards.py` 及其测试、`p59_http_evidence.py` 和实验工作流。基准的生产诊断、fixture 与 runner 配置按冻结版本一起核对。 | 先运行纯传输校准，再跑冻结容量基准；核对客户端调度/实际上线、服务端接收/准入/结果、逐项错误与完整业务内容。 |
| 集合读、微批和多队列研究 | 保留在 `experiments/db_execution`、矩阵基准与独立报告中。正式 HTTP 开关无此依赖。批量读取的独立 JOIN 与 Kernel 的 `joined_task_status` 仅供对照。 | 若将来接入其他入口，须在该入口重新过双向吞吐、尾延迟、角色隔离与恢复门槛。 |

候选代码分多个远端提交形成，整条提交会同时带入实验运行器；按上述依赖移植可避免将否决变体带入发布。此开关无需 DDL、数据回填、队列迁移或额外服务。启用前应跑完整业务回归、两库 S6、有 fsync 的 PG 急停和 PITR、Redis 停机重建及原生 Windows 用例；时间线、历史压力和两种一小时负载继续独立报告。[任务详情测试](../../tests/integration/test_p59_fenced_status.py) · [启停及恢复原始记录](../../reports/performance/p59_database/raw/validation/README.md)

### 正确性覆盖与边界

| 验证对象 | 可复验的行为 |
|---|---|
| 授权、任务与数据隔离 | 同批内的错误不影响合法请求、撤权与共享锁排队后重查、原生任务返回等价、实际 HTTP 启动开关与其他入口隔离。[协调器用例](../../tests/integration/test_p59_db_candidates.py) · [单项锁定与绑定](../../tests/integration/test_p59_fenced_status.py) |
| 世界版本、Scope、知识切面 | 固定版本时间线逐请求校验；不同切面、Scope 冲突、正式世界更新与恢复走原流程。哈希实验另注入真实数据库内容变动并检查返回对象独立性。[查询覆盖](../../tests/integration/test_s9_query_coverage.py) · [Scope](../../tests/integration/test_scope_identity.py) · [世界内核](../../tests/integration/test_world_kernel.py) · [哈希反例](../../tests/integration/test_p59_revision_witness.py) |
| 取消、期限、事务与重试 | 排队撤销、单项拒绝、批回滚、失联处理均有直接断言。真实停库时同一凭证收到 200 → 503（Retry-After 为 1）→ 200；恢复后的完整 JSON 等价。UNKNOWN 保留且未重复扣费。独立物理 PITR 核对世界、模型费用账本及 Redis 投递。[协调器用例](../../tests/integration/test_p59_db_candidates.py) · [物理恢复记录](../../reports/performance/p59_database/raw/validation/) |
| 调度公平与资源 | 两部作品的工作流结果逐项核验；记录主动唤醒、代际、领取、取消、UNKNOWN 和持久模型账本。多 worker、热点混合与四背景读者对照均保留。有限观测不能证明无限时间下无饥饿或死锁。[五档工作流](../../reports/performance/p59_database/raw/actions/43288881002/) · [抢占资源对照](../../reports/performance/p59_database/CANDIDATE_CONFIRMATION.md) |
| 跨平台与回归 | 定稿原生 Windows Server 2025 + PG17 的 35 项全部通过。Linux 上实际停止并重启隔离 Redis，两个通知与重放用例均通过。[平台与 Redis](../../reports/performance/p59_database/raw/actions/43294749796/) |

### 原始负载的通过项与失败项

| 冻结业务与负载 | 本轮观测 | 验收 |
|---|---|---|
| 50 / 500 身份，原始混合节奏 | 286 / 2,851 项计划操作；分别有 4 / 49 个预期的 409 冲突，产生 6 / 51 个独立世界提交。基础设施重试均为 0。 | 两档均通过。 |
| 50 身份过载 | 首次业务调用开始 91.08/s，开始发头 51.16/s。首次返回 470 个 200、4 个 409、62 个 429。 | 950/s 容量未过，有界恢复与 6 次世界提交通过。 |
| 200 身份过载 | 首次调用开始 40.14/s，开始发头 32.86/s。首次返回 1,621 个 200、19 个 409、501 个 429。按冻结预算进行了 851 次基础设施重试，核验 21 次唯一世界提交。 | 950/s 容量未过，有界业务恢复通过。 |
| 1,000 身份，原始开环单次时间线 | 5,000 项提供，1,702 项核验通过，3,284 个 429，14 个传输错误；开始发头 42.16/s，有效 14.09/s。 | 历史容量未过。 |
| 500 身份，600 秒原始持续读 | 12,000/12,000 项通过，约 20.00 项/秒，新增世界提交 0。 | 十分钟探针通过。 |

冻结过载工具允许的有界恢复与开环单次物理尝试使用不同分母；全程状态、结果时延及原始退出码可分别查看。额外探针和真实任务 GET 的更改均未调整上述容量门槛。[50/500 原始三种负载](../../reports/performance/p59_database/raw/actions/43294345517/) · [200 过载、1,000 开环](../../reports/performance/p59_database/raw/actions/43292819006/) · [两个历史源对照](../../reports/performance/p59_database/HTTP.md)

### 源码、环境与分支兼容

[源码与树 SHA 对照](../../reports/performance/p59_database/source-manifest.json) 将每个使用 GitHub runner 的试验映射到可检出的源码。相同树在本地与远端的提交身份可以不同。冻结基线作业另记录实际检出的被测提交，不能将其与承载 workflow 的提交混用。原始归档分别含时长、数据配置、程序输出及适用的平台/数据库版本。[运行登记](../../reports/performance/p59_database/archive-runs.json) · [原始文件](../../reports/performance/p59_database/raw/actions/) · [本机验证环境](../../reports/performance/p59_database/raw/validation/README.md)

PR #33 在实验途中前进。以最新核验的 `877be80d7c1dfa8b6fb9c9c5a2a570c7b14d3ca0` 与候选定稿生成未发布的组合树 `cfaadaf9007fa129985f2d5c722c7ba40915e69e`，无冲突。相对上次 `293e6f4c`，上游新增的 9 个文件差异仅为文档与数据。组合树的产品源、迁移、实验与基准均与定稿一致。该树用真实 PG17.11 运行 62 项检查，59 通过，3 个 Windows 专有用例按平台跳过。覆盖上游已变动的 Provider 用例。候选、运行日志、否决记录和环境说明统一保存在一条实验分支，没有建立待合并的候选分支。[兼容性收据](../../reports/performance/p59_database/raw/compatibility.json) · [兼容性 JUnit](../../reports/performance/p59_database/raw/compatibility.xml)
