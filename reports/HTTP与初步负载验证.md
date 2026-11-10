# P1 HTTP 接入与初步负载验证

## 实现边界

`src/novel_factory/interfaces/http.py` 的 `create_app(kernel)` 把 HTTP 请求交给同一 Kernel、
WorldQueryService 和 ContextCompiler。HTTP 不修改数据库表，不接受客户端自报的 Principal，
不提供任意 SQL、文件路径、shell 或世界初始化工具。身份由 opaque Bearer token 查找当前数据库 grant。

本轮实现时间查询、同切面批量查询、上下文编译、任务创建/领取/查询/取消/结果存储、
封闭模板提案、接受证书、strict/guarded 接受、分支和推广入口。原 core/R1 DTO 不重定义；
新增 HTTP 包装层的维护源为 `contracts/http.schema.json`，提案命令引用 `commands.schema.json`。
两者一起生成到统一 Schema bundle。

请求体在解析前受大小和并发总量限制，拒绝重复 JSON key、非有限数字、多余字段和不获准 Origin。
所有业务错误采用 ErrorEnvelope；数据库异常和未预期异常只返回安全说明，日志不回显 token、SQL 参数或正文。
HTTP 传输断开不自动撤销已完成的世界提交。已进入工作线程的短数据库调用完成后才释放并发槽，
避免客户端取消造成仍在运行的事务越过池预算。

## 资源控制

`interfaces/admission.py` 提供按 Principal 轮转的有界异步队列、单 Principal 并发上限、
query/context/write/task 类别上限、等待超时与取消清理，以及独立 token bucket 速率限制。
这些控制发生在获取业务数据库连接之前。认证与业务操作的并发预算之和不超过单 API 进程池大小。

队列满返回 HTTP 429 / BACKPRESSURE，速率超额返回 HTTP 429 / RATE_LIMITED；返回 Retry-After。
配置层计算所有声明 API/worker 进程与维护预留的数据库连接总上界，超过 PostgreSQL 上限则拒绝启动。
不同进程的队列分别持有自己的配置份额，持久任务、权限和世界提交资格仍由 PostgreSQL 验证。

## 实际执行

本轮使用真实 PostgreSQL 17.11，HTTP 请求通过 loopback TCP 到 Uvicorn，不是 ASGI TestClient
替代网络。每个逻辑 Agent 具有独立 grant、实际创建并领取的任务及持久 ContextBundle。
模型思考仅以客户端短等待模拟，未调用外部模型。

| 测试组 | 数量 | 结果 |
|---|---:|---|
| Admission 单元测试 | 7 | 通过：轮转、公平份额、分类容量、满队列、取消、超时、速率恢复、总连接预算 |
| HTTP 与 PostgreSQL 集成 | 10 | 通过：鉴权、字段/大小/Origin、角色平面隔离、跨任务拒绝、撤权缓存、取消迟到、限流、背压、错误脱敏、两个独立 API 进程、完整 TCP 正式提交与幂等重试 |
| 实际 HTTP 负载 | 3 | 通过：10 Agent、50 Agent、独立到达负载 |

最终完整验收记录为 `integration/release_verified.*`：237 passed、0 failed、0 skipped，退出状态 0；其中本报告的服务测试为 20 项，均通过。量测文件使用 `load_tests/release/`；较早失败、成功和中断运行分别保留，不混计为最终通过数。

每组原始 stdout、JUnit XML 和 pytest JSON 报告在交付 `evidence` 中。原始首轮 HTTP 负载失败也保留：
httpx 继承了执行器的 SOCKS 环境代理；loopback 测试明确使用直接本地连接后复验通过，没有添加虚假的成功记录。

## 初步量测结果

下表对应最终运行的 `load_tests/release/http_agents_10.json`、`http_agents_50.json`、`http_open_loop.json`；较早独立批次保留在上一级目录。
这些数字是在一轮小数据试验中观测的结果，不是生产容量承诺或严格的尾延迟 SLO。

| 工作负载 | 计划/完成/正确请求 | 有效请求/秒 | P50 | P95 | P99 | 实际 SQL 语句 | 连接峰值/结束 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 10 Agent，6 轮闭环 | 60 / 60 / 60 | 36.4529 | 249.4519 ms | 320.9903 ms | 343.3919 ms | 1,260 | 8 / 0 |
| 50 Agent，6 轮闭环 | 300 / 300 / 300 | 34.0342 | 855.6447 ms | 2,724.2681 ms | 4,274.2417 ms | 6,300 | 8 / 0 |
| 10 Agent，计划 150 请求/秒的短独立到达 | 100 / 100 / 100 | 35.3375 | 468.9213 ms | 1,439.0637 ms | 1,687.6771 ms | 2,100 | 7 / 0 |

独立到达试验的计划到达率与完成吞吐不同，短突发在服务中排队后完成；不能称为已持续承载 150 RPS。
所有响应延迟与成功响应延迟分别统计，拒绝/错误/未发出也保留字段，未将快速拒绝算作有效吞吐。

本次数据为 R1 青霜剑初始 D1：22 个实体及初始关系，尚未加载上万事件。
请求查询固定 WorldRef 的所有权和实际持有，直接核验测试定义中的初始主人，并核验每个返回切面。
所有测试请求是查询，实际世界 revision 增量为 0；这不代表世界接受 TPS。
每轮全体 Agent 进入模拟思考屏障时，连接池 checkout 为 0，结束同样为 0。

两个独立 Uvicorn 进程另行验证同一持久任务和授权状态：两端均返回相同查询结果，撤权后两端均返回 401。
该测试每进程池为 4，API 总上界为 8；进程 PID、响应状态和传输类型保存在 `load_tests/release/two_api_processes.json`。

## 可复现命令

在项目根目录，已具备 PG_BIN 和锁定虚拟环境时运行：

```bash
uv sync --locked
export NF_LOAD_EVIDENCE_DIR="$PWD/../reviewer_evidence/load_tests"
.venv/bin/python scripts/run_recorded.py ../reviewer_evidence/concurrency_tests/http_load \
  -- .venv/bin/python scripts/with_postgres.py --pg-bin "$PG_BIN" \
  --log-dir ../reviewer_evidence/concurrency_tests/http_load_pg \
  -- .venv/bin/python -m pytest tests/service -q \
  --junitxml=../reviewer_evidence/concurrency_tests/http_service.xml \
  --json-report --json-report-file=../reviewer_evidence/concurrency_tests/http_service-results.json
```

普通环境可以使用已经启动的 PostgreSQL，设置 NF_TEST_DATABASE_URL 后直接执行 pytest。
测试数据库账号需要在隔离集群内创建/删除测试数据库的权限。禁止把真实生产数据作为该测试目标。

## 剩余范围

未量测 10,000+ 事件历史、长期持续运行、完整混合模型调用链、200/500/1000 Agent、跨机器对象共享、
TLS 反向代理或完整 MCP/外部 Agent Host；没有把这些范围标为已验收。50 Agent 的短测尾延迟已经明显升高，
后续扩展需依据持久任务/SQL 成本和排队数据继续评估。本轮不进入 P2。
