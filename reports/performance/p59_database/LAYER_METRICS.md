# 十层实测指标与原始样本索引

HTTP 开环每轮计划在 5 秒内发送 5,000 次请求。纯传输与健康检查分别验证了 5,000 个响应，真实鉴权验证了 2,472 个身份，鉴权加任务状态验证了 2,447 个结果，完整 timeline 验证了 93 个结果。后面三层的其余请求均收到 HTTP 429。下表同时保留成功与背压请求的延迟，便于从某一层直接追到原始样本。

## 调用、正确性与延迟

延迟单位为毫秒，三项依次为 P50、P95、P99。第 1–5 层从计划发送时间算到读完响应，含客户端等槽和服务端排队，有效吞吐以最后一个响应读完为截止。其他层的计时范围见各行。每个链接均指向仓库内留存的原始 ZIP。

| 层及入口 | 身份、样本及正确性 | 延迟 P50 / P95 / P99（ms） | 校验通过的吞吐 | 原始样本 |
| --- | --- | --- | ---: | --- |
| 1. 纯 HTTP 传输 | 固定响应接收器，无业务鉴权。5,000/5,000 次 HTTP 200，逐个核验 2,104 字节响应 | 41.62 / 41.68 / 42.66 | 990.94/s | [timeline 同机首轮四分片校准](raw/actions/43289598993/13976638692.zip) |
| 2. 健康检查 | 真实 `GET /health`，按身份计划携带令牌，接口公开。5,000/5,000 次 HTTP 200 | 41.76 / 41.84 / 42.80 | 992.30/s | [minimal](raw/actions/43289598993/13977690034.zip) |
| 3. 鉴权及准入 | 实际令牌、限流与操作准入，测试处理函数回显 principal ID。2,472 个正确 ID，2,528 个 429 | 83.19 / 656.22 / 756.21 | 481.52/s | [auth](raw/actions/43289598993/13975969617.zip) |
| 4. 鉴权及任务状态 | 真实 `GET /api/v1/tasks/{id}`，每个身份读自己的任务。2,447 个正确结果，2,553 个 429 | 1,681.48 / 3,167.42 / 3,359.45 | 295.34/s | [auth-read](raw/actions/43289598993/13976104080.zip) |
| 5. 完整固定版本 timeline | 真实 `POST /api/v1/queries/timeline`，93 个成功结果通过当时冻结的响应校验，4,907 个 429 | 1,809.62 / 3,857.25 / 5,926.61 | 8.88/s | [query](raw/actions/43289598993/13976638692.zip) |
| 6. MCP stdio | 两个独立绑定身份，各自启动生产 MCP 子进程。每阶段每身份 60 次，交替读自己的任务和上下文；闭环与开环各 120/120 正确，0 错误。起点为闭环发起或开环计划到达 | 闭环 36.17 / 43.71 / 51.41；12/s 开环 20.08 / 23.41 / 26.46 | 闭环 107.35/s；开环 12.08/s | [result.json.gz → mcp](raw/actions/43292819006/13976644977.zip) |
| 7. 进程内 Agent | 与第 6 层相同的两个身份、工具和预期内容，直接调用 `invoke_bound_tool`；每身份独立串行 60/60 正确，0 错误。延迟含返回内容核验 | 身份 1 为 16.01 / 21.00 / 23.53；身份 2 为 17.08 / 20.42 / 23.34 | 59.95/s；59.27/s | [result.json.gz → internal_agent](raw/actions/43292819006/13976644977.zip) |
| 8. PostgreSQL SQL 微基准 | psycopg 同一会话的 TEMP 表，无业务授权。读、插入、更新、冲突更新 × 六种批量 × 三种方式，每种组合 2 次。144 个事务计时，共核对 5,976 行；另验一次约束错误后整批回滚 | 每组合仅 2 次，未报告尾分位。32 键读取的两次总耗时分别为逐行 1.855 / 2.417，pipeline 1.904 / 0.758，集合 SQL 0.375 / 0.341 | 32 键集合读取 85,308 / 93,809 行/s | [protocols.json.gz → records](raw/actions/43292819006/13976644977.zip) |
| 9. 工作流领取与结果状态 | 两作品、50 个任务 principal、2 个执行槽。对 50 个领取逐个核验代次和持久化状态；49 个有效结果，另 1 个按预设进入 PAUSED/UNKNOWN。无后台读取的第一轮 | 创建至领取 4,121.49 / 5,836.66 / 6,002.34（50）；调度准入调用至结果 211.57 / 316.45 / 416.10（48） | 排空 7.83 个有效结果/s；含准备 6.22/s | [busy.json.gz → 第 1 轮](raw/actions/43291814205/13977811101.zip) |
| 10. 争用中的工作流 | 同一 50 任务工作流与 4 个持独立 writer 授权的读取线程共享池。首个争用轮 49 个有效结果，1 个预设 PAUSED；后台 7,788/7,788 个状态正确 | 创建至领取 10,302.03 / 15,962.65 / 16,470.87（50）；调度准入调用至结果 621.35 / 713.75 / 751.51（48）；后台读 10.09 / 18.32 / 26.98（7,788） | 排空 2.89 个有效结果/s；含准备 2.30/s | [busy.json.gz → 第 2 轮](raw/actions/43291814205/13977811101.zip) |

第 1–5 层使用 4 个 h11 负载进程、总计 256 个客户端连接槽。第 2–5 层各在独立 runner 上配置 2 个 HTTP 服务进程、合计 8 个应用数据库连接，按 1,000 个 fixture 身份各安排 5 次请求。各层超时、传输错误、409、5xx、客户端自动重试均为 0。第 5 层的原版校验覆盖冻结世界版本和预期 owner；后续增强版另核对请求所指定的 subject、predicate 和 knowledge cut。两版证据的归属见[HTTP 校验与修订记录](HTTP.md)。

第 6–7 层均交替调用 `nf_task_get` 与 `nf_context_get`，按角色逐项比对完整结构化结果，外部模型调用为 0。第 9–10 层用进程内适配器完成 50 次确定性模型调用，商业模型调用为 0。创建至领取取数据库任务的 `created_at` 到第一条 RUNNING outbox 的时间差。程序先执行两项前置任务，随后对另外 48 项测量 `admit_model` 发起至取得其结果的时长。各轮均逐任务核验领取次数、代次、唤醒回执、当前结果及模型账本状态。单条领取 SQL 的墙钟延迟未在此工作流中单独计时。[绑定工具对照](../../../benchmarks/p59_db_entrypoints.py) · [工作流指标实现](../../../benchmarks/p59_workflow_load.py)

## 资源、等待及错误

CPU 单位为进程 CPU 秒（用户态加内核态），RSS 单位为 KiB。第 1 层服务侧取固定接收器。第 2–5 层 CPU 为两个 HTTP 服务进程的测量区间之和，服务 RSS 为两进程各自生命周期峰值之和。客户端列给 4 个发送进程的区间 CPU 总和及单进程最大 RSS；该版未采集父进程验收 CPU。PostgreSQL 后端的独立 CPU、RSS 及全机同步总 RSS 均未采集。

| 层 | CPU / RSS（按进程范围） | 数据库连接和 SQL | 等待与错误 |
| --- | --- | --- | --- |
| 1. 纯传输 | 接收器 1.444 s / 71,808；发送侧 2.027 s / 74,176 | 无业务数据库 | 客户端等槽 P95 0.0111 ms；接收器断连、处理错误均 0 |
| 2. 健康检查 | HTTP 服务 2.261 s / 317,424；发送侧 1.683 s / 158,712 | 应用池借出峰值和 0，SQL 0；PG 采样连接峰值 0 | 鉴权 / 业务准入累计等待 0 / 0 s，客户端等槽 P95 0.0098 ms。观察到 Lock 的采样 0/31 |
| 3. 鉴权 | HTTP 服务 10.290 s / 316,552；发送侧 1.720 s / 158,276 | 应用池借出峰值和 4，SQL 2,472；PG 采样连接峰值 4 | 鉴权 / 业务等待 541.83 / 0.014 s，客户端等槽 P95 0.0101 ms。Lock 采样 0/35；2,528 个 BACKPRESSURE |
| 4. 鉴权及任务状态 | HTTP 服务 16.687 s / 317,144；发送侧 2.279 s / 158,572 | 应用池借出峰值和 8，SQL 14,315；PG 采样连接峰值 8 | 鉴权 / 业务等待 759.19 / 965.31 s，客户端等槽 P95 2,735.46 ms。Lock 采样 0/52；2,553 个 BACKPRESSURE |
| 5. 完整 timeline | HTTP 服务 20.168 s / 317,424；发送侧 2.804 s / 158,712 | 应用池借出峰值和 8，SQL 1,673；PG 采样连接峰值 8 | 鉴权 / 业务等待 669.71 / 94.61 s，客户端等槽 P95 2,693.46 ms。Lock 采样 0/66；4,907 个 BACKPRESSURE |
| 6. MCP stdio | 两阶段合计且含初始化，客户端 CPU 0.256 s、已退出子进程 CPU 总和 5.068 s；客户端峰值 97,448，最大已退出子进程峰值 97,192 | 两阶段结束时每个绑定各 2 个数据库会话；观察到的同库会话峰值 5。每工具 SQL 次数未采集 | Lock 采样 0/62；SQL 池等待时长未采集；两阶段均无错误响应或丢弃 |
| 7. 进程内 Agent | 两身份各自 60 次调用的进程 CPU 0.716 / 0.710 s，阶段结束时同一进程生命周期 RSS 峰值 96,296 / 96,680 | 每身份 2,100 SQL，300 个事务，应用池借出峰值各 1，归还后 0；同库采样会话峰值 2 | Lock 采样 0/11；SQL 失败各 0；获取连接的单独等待时长未采集 |
| 8. SQL 微基准 | 进程峰值 53,768；32 键读取两次的客户端 CPU（ms）为逐行 1.000 / 1.004，pipeline 1.786 / 0.681，集合 0.143 / 0.118 | 应用连接 1，driver queue 为 0；32 键每次读取执行 SQL 32 / 32 / 1 条，排除 fixture、事务控制 | 单会话 TEMP 表，无跨会话争用；服务端锁等待时长未采集。预设重复键失败后留存行数 0 |
| 9. 工作流领取 | 驱动与服务同进程 6.527 s / 96,428（首轮） | 应用池借出峰值 4/8；同库采样峰值 4；15,936 SQL，失败 0 | Lock 采样 0/39；0 个错误任务，结束剩余槽 0 |
| 10. 争用工作流 | 驱动、服务、后台线程同进程 24.065 s / 102,188（首个争用轮） | 应用池借出峰值 8/8；同库采样峰值 8；47,088 SQL，失败 0 | Lock 采样 0/106；0 个错误任务，结束剩余槽 0。后台四身份各完成 1,945 / 1,933 / 1,947 / 1,963 次读取 |

表中每个 Lock 分母为约每 200 ms 一次的 `pg_stat_activity` 采样数，分子为出现过至少一个 `wait_event_type='Lock'` 的样本数。观察器排除自身连接，保留同库空闲会话；短时等待可以落在采样间隙。应用池借出峰值和取各进程各自的高水位，鉴权与业务的等待秒数分别对所有请求累计，并发时会重叠。第 2–5 层的应用 SQL 失败数及收尾借出连接数均为 0。[连接采样](../../../benchmarks/p2_behavioral_load.py) · [服务进程计数](../../../benchmarks/process_server.py)

第 9–10 层的原始顺序为 0、4、4、0 个后台读取线程。四轮有效结果数均为 49，最终状态均为 47 个 RESULT_STORED、2 个 ACCEPTED、1 个 PAUSED。排空吞吐依次为 7.83、2.89、2.81、8.66 个有效结果/s。第二个争用轮的准入至结果 P95 / P99 为 769.65 / 976.73 ms，最后一个空闲轮为 300.83 / 307.40 ms。后台线程在另行建立的 fixture 任务上读状态，双方争用同一个 PG 池；同一业务行上的锁等待未被单独注入。[四轮明细](raw/actions/43291814205/13977811101.zip)

## 核查原始字段与复现

| 层 | ZIP 内成员与原始字段 | 采集时源码 |
| --- | --- | --- |
| 1–5 | 第 1 层取 `calibration-1.json.gz`，第 2–5 层各取 `business.json.gz`。逐次请求在 `raw_requests`，延迟和失败在 `summary`；发送侧取 `client_workers`，接收侧取 `server_metrics`，连接和锁取 `database_observer.samples[].connections` | `be14de6a`，[原运行命令](../../../.github/workflows/p59-db-http-shards.yml) |
| 6–7 | 同一 ZIP 中的 `result.json.gz`。MCP 取 `mcp[].samples`、`mcp_resources`、`mcp_database_observer`；进程内取 `internal_agent[].latencies_ms`、`.sql`、`.cpu_seconds` 与 `internal_database_observer` | `d9a0df49`，[原运行命令](../../../.github/workflows/p59-db-final-checks.yml) |
| 8 | 同一 ZIP 中的 `protocols.json.gz`。取 `records[].{operation,method,keys,elapsed_ms,cpu_ms,statements,verified_rows}`、`pipeline_failure_rolled_back` 与 `peak_process_rss_kib_linux` | `d9a0df49`，[执行与回滚校验](../../../benchmarks/p59_sql_protocols.py) |
| 9–10 | `busy.json.gz` 为四轮数组。取 `workflow.records`、`creation_to_claim`、`admission_to_result` 等 workflow 字段，外层 `read_latency`、`read_counts`、`db`、`observer.samples` | `05751855`，[原运行命令](../../../.github/workflows/p59-db-confirm.yml) |

在对应源码版本的 Linux 环境使用锁定依赖与独立 PostgreSQL 17 数据库；CI 工作流列出了建库、环境变量和原始运行次序。以下为各层程序的调用参数。HTTP 各业务层原测量使用各自独占的 runner，`transport` 校准在同机先跑。第 6–10 层需已设置 `NF_TEST_DATABASE_URL`。

```bash
uv run --no-sync python -m benchmarks.p59_http_shards --layer "$LAYER" --workers 4 --rate 1000 --agents 1000 --rounds 5 --connections 256 --servers 2 --output "out/$LAYER.json"
# LAYER 分别为 transport、minimal、auth、auth-read、query
uv run --no-sync python -m benchmarks.p59_db_entrypoints --samples 60 --output out/bindings.json
uv run --no-sync python -m benchmarks.p59_sql_protocols --output out/protocols.json
uv run --no-sync python -m benchmarks.p59_contended_workflow --agents 50 --output out/busy.json
```
