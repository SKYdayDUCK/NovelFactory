# 两作品的 MCP 工作流与 Workshop 委托

## 有实测收益的变更

Workshop 的 `aw-reconcile` 原来为每个 NF 委托操作启动一个 stdio 子进程，初始化 SDK，发现可用工具，再发起一次调用。实验选项 `--reuse-session` 在同一安装、同一组织、同一次有界 CLI 批次内延迟建立一个连接。每个已领取操作仍做一次实时发现，服务端每个工具仍在事务中验证安装、作用域、当前 grant 和实际权限。未领取工作时不会启动 NF 子进程。远端出错会关闭失效连接，由后一个独立操作重新建连。当前操作的未知结果仍走原有数据库恢复和回执核对。一个批次结束后 CLI 回收其连接。

| 实际业务与臂顺序 | A1 | B1 | B2 | A2 | 两对 B/A | P95/P99 两对 B/A |
|---|---:|---:|---:|---:|---|---|
| 两个安装读取已确认回执，每臂 8 次，正确回执/s | 0.478 | 2.077 | 1.898 | 0.531 | 4.343× / 3.578× | 0.685 / 0.810 |
| 一个安装领取并创建 4 个不同的新委托，每臂 4 次，正确 BOUND 任务/s | 0.687 | 1.535 | 1.472 | 0.509 | 2.234× / 2.894× | 0.924 / 0.825 |
| 相同新委托基准，独立 PG 复验，正确 BOUND 任务/s | 0.382 | 1.041 | 1.087 | 0.427 | 2.728× / 2.548× | 0.802 / **1.100568** |

创建路径在两组完整 A/B/B/A 中，四个相邻对照都提高了实际成功吞吐，三个同时通过时延门槛。独立复验的最后一对，P95/P99 比率 1.100568 超过预注册上限 1.10，整组按未通过记录。阈值未取整、未重设。批内复用作为显式实验选项交付；面向所有委托写入的通用默认值仍缺少稳定尾延迟证据。[独立复验逐项原始数据](delegation-create-repeat.json)

A 为默认新开连接，B 为批内复用。每个实验臂的完成吞吐包含启动和全部连接关闭的时间。创建实验在两个独立数据库、两个受到权限约束的服务账号上运行。计时从普通 Workshop CLI 启动开始，到全部操作持久化且进程退出为止。每个 BOUND 都在两个数据库中核对预授权任务 ID、NF READY、AW 回执及一笔 HELD 预算。首轮和复验各 16 个，合计 32 个任务均匹配，零 Provider Attempt。读取实验验证全部 32 个返回的完整回执，并用已打开的关联会话在撤权后再次发起请求，收到 VIEW_DENIED。

每臂只有 4 或 8 个样本，最近秩 P95 和 P99 都等于该臂最大值。测量保留第一个冷调用。同机同时有另一工作流测试，创建与读取固定在 CPU 8。读和写的优势都出现在有相邻任务可共享启动开销的短批次；单个调用没有摊销机会。保留单次连接作为默认值，不应将这两个比值用于其他角色或远程部署。关闭实验只需移除 CLI 的 `--reuse-session`，无迁移或常驻组件。

原始数据与预注册见 [读取](delegation-abba.json)、[创建](delegation-create-abba.json) 和 [实验方案](REUSE_PROTOCOL.md)。创建实验运行时 HEAD 是 `47ef968`，当时的逐字节驱动和后续提交 `151df2b` 的 `benchmarks/p59_mcp_delegation_create.py` 相同，SHA-256 为 `3f0ed5f8569328a817fa05e9efea2b7fad1baf3573942772a0a79146804a9f0c`。读取实验运行于 `aca728d`，原始文件另有两个执行源码哈希。

## 工作流容量的业务边界

每个参与者都有真实 PostgreSQL principal、grant、task、不可变上下文和一次独立终态校验。两个项目级 MCP 绑定在同一时刻各对应一个真实官方 SDK 会话、一个 stdio 进程和一项当前作品的授权。每本书只有两个项目控制工具。首次建连、实时发现、状态读取、依赖恢复、排空后断开和重连走 MCP。对初始和排空后的恢复请求逐次重放验证，并比较每次返回和属于该作品的实时 SQL 计数。创建、领取、租约、预算、发送、记账、存储和接受两项前置成果走共享应用和 Supervisor。每个逻辑 Agent 不单独启动 MCP 进程。每个规模有四次初始化握手，最多两个同时存活。计时保留每项任务的排队、运行、终态和回执；依赖恢复使用真实 `nf_resume_project`，每次最多处理 100 项。

每 100 项注入一项发送后响应丢失，保持 UNKNOWN 及 PAUSED，另一项在模型发出前取消。每项成功结果都属于当前代际和上下文。两本书各有一项证据摘要被接受，其他成功返回保存在 RESULT_STORED。每个 Agent 的等待、唤醒、有效结果、重复领取、调用次数及世界 head 在数据库中单独对账。零商业模型调用。模拟器的等待有长短差异，不能代表商业模型的服务时间分布。人工待审在独立回归中校验，规模实验的等待是证据前置依赖。

Linux、Python 3.12.14、MCP 1.29.0、PostgreSQL 17.11、Psycopg 3.3.8、SQLAlchemy 2.0.57。每档重新建立 PG，128 条服务端连接上限，启用 pg_stat_statements 与 I/O 计时，应用主池 8、overflow 0。角色工作流限 2 个 worker，模拟 Provider 峰值 2。每个 project MCP 进程另有 2 个池槽。工作流进程和 PG 子树绑 CPU 6–8。不同的 CPU 亲和性仍共享内存、缓存和磁盘；原始数值用于容量和正确性复验，不构成相对历史版本的性能提升对照。新旧基准跨机器比值不参与选型。

## 独立恢复与授权回归

| 证据 | 通过 | 内容 |
|---|---:|---|
| [授权与恢复](workflow-regression.xml) | 22 | 真 SDK 角色知识隔离、续权/撤权、空闲连接、Supervisor 进程重启、已发 UNKNOWN、编辑等待、Redis 断连及重建 |
| [内容、预算与调度](control-domain.xml) | 22 | 真 SDK 审读、提交及幂等重放；场景素材应用读取、模型等待时的数据库活动、有界调度、跨作品 token 和 UNKNOWN 预算 |
| [委托与角色交接](delegation-authority.xml) | 18 | 真实提交返回域任务 ID，丢失回复后查询同一凭证；首次领取到实际结果；撤权、过期、重复操作和冻结恢复 |
| [两服务实测](dual-both.xml) | 2 | 冷启动与复用 CLI 均能恢复已确认任务，两个服务账簿对账并从 PG 重建 Redis |
| [传输故障单元](batch-fault.xml) | 1 | 原始提交失去回复时不会触发重送，下一项操作建立新连接 |
| [真实 NF 子进程终止](peer-death.xml) | 1 | 终止已复用的 NF 进程，在新子进程中通过原始授权读取同一持久化回执 |

MCP 项目端对人工待审可见性与依赖唤醒的覆盖来自不同用例。当前 `nf_resume_project` 只恢复合法、已满足的依赖并更新队列状态，作者决策仍要求业务授权。此试验没有邀请人类参与审稿，未使用实际商业模型。

## 复现

以下命令在仓库根目录执行，Python 3.12、PostgreSQL 17 工具和仓库依赖可用；为 Workshop 源码设置 PYTHONPATH，NF 及强制包含的私有文件模块应通过仓库标准安装步骤安装。私有配置由测试在隔离目录内生成，临时凭证不进入报告。PG 管理 URL 须指向可创建临时测试库和服务角色的独立测试实例。双服务实验会核查各服务账号无法连入对方数据库。复验 Redis 故障需要独立 redis-server、redis-cli 和 NF_TEST_REDIS_URL。

```bash
uv sync --frozen --group dev
export PATH="$PWD/.venv/bin:$PATH"
export PYTHONPATH="$PWD/workshop/src:$PWD/src:$PWD"
export AW_NF_SOURCE="$PWD"

python scripts/with_postgres.py -- python -m benchmarks.p59_mcp_workflow --agents 500 --output /tmp/workflow-500.json.gz
python scripts/with_postgres.py -- python -m benchmarks.p59_mcp_workflow --agents 1000 --output /tmp/workflow-1000.json.gz
python scripts/with_postgres.py -- python -m benchmarks.p59_mcp_workflow --agents 2000 --output /tmp/workflow-2000.json.gz
python scripts/with_postgres.py -- python -m benchmarks.p59_mcp_delegation --output /tmp/receipts.json
python scripts/with_postgres.py -- sh -c 'AW_DUAL_TEST_ADMIN_URL="$NF_TEST_DATABASE_URL" python -m benchmarks.p59_mcp_delegation_create --output /tmp/tasks.json'
```

每个规模的 JSON 附原始 Agent 清单和 MCP 工具回包摘要。2,000 人工作流首轮发生共享主机进程被终止，没有产出可计的完成数据。[中断记录](INTERRUPTED.json)
