# HERA 执行架构

同一个 PostgreSQL 数据库保存任务、输入代次、租约、命令回执、预算和正式效果。业务入口可以是绑定的 stdio MCP、远程 Streamable HTTP MCP 或 REST。绝大多数改动在现有模块化单体中运行。安装范围内的 Workshop 委托沿用原有双数据库边界。

| 业务入口 | 身份与提交 | 谁执行 | 如何恢复 |
|---|---|---|---|
| 普通 TaskSpec | 身份由 bearer 或私有绑定认证。Kernel 在事务内复核作品、分支、角色、任务能力及 WorldRef | 受权外部 worker 通过原有 claim/result 接口领取并提交 | 同一授权重试幂等命令，或从另一个入口查询 Task ID |
| 安装预授权委托 | 服务端冻结任务模板、作品、角色、Provider、预算和安装 epoch。Delegations 用稳定 operation ID 在同一事务中写任务、Supervisor 登记和委托回执 | Supervisor 按持久配额领取。模型完成后由原有交付服务校验候选及授权 | 安装可重连并重取同一回执，Workshop 根据账本和订阅恢复 |
| 项目进度与恢复 | 仅有绑定作品、分支、有效期和政策版本的 resume_operator 可访问 | status 为短操作，resume 仅执行有界依赖扫描、唤醒和恢复 | 终态通知可提交精确回执与代际条件，再次检查授权 |
| 角色任务与上下文 | TaskBinding 固定任务、代次和 worker，读取时核对角色知识切面、上下文和授权 | 绑定工具沿用既有审计和角色工具白名单 | 固定 WorldRef 和重新绑定的代际分别处理 |

内部可信调用由已认证的 Principal、服务端绑定、规范化操作及通过 schema 校验的参数组成。写入身份与原有 command receipt、task input、provider attempt 和 invoice 分别对应。协议来源只用于诊断。涉及模型预订、花费或正式接受的命令保留现有事务、锁和幂等键。

## 接入与执行

进程有界缓冲先处理认证与报文大小。认证、短操作和速率限制由两个远程协议共享。可用 NF_HERA_FAST_SLOTS 为轻量查询和取消预留操作槽；保留至少一个通用槽，默认预留零。该配置只切分服务端短操作容量，无法单独消除数据库锁、数据库 CPU 或其他进程的竞争。跨入口普通任务的提交返回持久回执，响应在提交成功后发送。合法请求超出可信排队上限、截止期或身份配额后收到背压。

复杂工作按数据库领取、提交、模型调用、计算与通知分别推进。等待依赖或审核的任务记录在 PostgreSQL；每个已发送但未确定的 Provider Attempt 只能通过对账结束。模型调用期间归还数据库连接。详见[准入策略](HERA_DURABLE_ADMISSION.md)、[接口契约](HERA_UNIFIED_MCP_HTTP.md)、[执行配额](HERA_RESOURCE_SCHEDULING.md)、[缓存](HERA_REQUEST_CACHE.md)和[通知桥](HERA_EVENT_BRIDGE.md)。

## 进度投影

对单个分支可运行 `python -m scripts.workflow_projection rebuild WORK_UUID BRANCH_UUID` 建立计数。重建期间写锁会暂时阻止该表的任务更新，应计入运维窗口。启用 `NF_HERA_PROJECT_PROGRESS=1` 后，每次查询先执行原有的 PostgreSQL 当前授权。在同一语句快照中结合投影和未到期的重试计时器计算可运行任务。投影没有该分支时使用原始统计。事务触发器同时更新计数及本地 watermark，事务回滚会撤销两者。流程状态继续由相同的 WorkflowService 计算。恢复扫描及事件消费仍读权威任务行。投影上的统计不含角色正文和私有候选。

## 部署、回退与连接预算

执行 NF 和 Workshop 各自现有的 Alembic 迁移，再分阶段启用新开关。关闭进度读取后可用 `python -m scripts.workflow_projection disable WORK_UUID BRANCH_UUID` 删除该分支的派生计数和维护成本。停用查询合并、可选联查、远程委托或远程 roster 不会移除任何已接纳任务。退回旧二进制之前先处理尚在生效的工作流和队列策略；部分 schema 降级在存在活动等待、配额或订阅时会拒绝操作。

连接预算的配置总数由 API 池、Worker 池、维护预留、每个活动 stdio 进程两条连接、每个 compute 子进程一条连接、以及 LISTEN 长连接组成。分别设置 NF_ACTIVE_STDIO_PROCESSES、NF_COMPUTE_CHILD_PROCESSES、NF_LISTEN_CONNECTIONS 并在部署时计入全部进程。数据库角色可设置统一的 CONNECTION LIMIT 作为最终硬上限。各个进程不能自行发现其他机器的进程数。多副本测量使用固定部署清单，具体预算随报告保存。

[预登记指标](../../reports/performance/hera/BASELINE.md) · [验收矩阵](../../reports/performance/hera/FINAL_ACCEPTANCE.md)

用 `python -m scripts.workflow_projection check WORK_UUID BRANCH_UUID` 在同一 PostgreSQL 语句快照中对比投影和权威任务总数。已启用且一致时输出 OK，未启用时输出 DISABLED，不一致时输出 MISMATCH 并以 1 退出。发现不一致可执行同一命令的 rebuild 或 disable。计数格式损坏时读取回退到原任务统计；若任务状态变化无法为损坏的计数应用增减，0026 迁移会移除受影响的派生行，让正式事务正常提交。格式仍合法但数值不实的计数需要上述核对命令识别。

## 2026-11-13 独立性能复验的可选路径

| 配置 | 启用条件、状态边界和回退 |
|---|---|
| `NF_HERA_FUSE_STATUS_AUTH=1` | 已安装普通任务绑定、宿主工具白名单允许的合法单项状态读取，可在业务读取的同一授权检查中确认 READ。每次请求仍有 bearer 鉴权、作品和 grant 行锁、持有人和分支检查、开始及结束审计及两份 Outbox。其他绑定或无效参数继续按原通路检查。置 0 并重新启动服务恢复。 |
| `NF_HERA_COMPACT_CREATE=1` | Kernel 对私有 TaskSpec 副本做完整校验，在同一写事务中复用已锁定的作品政策。无输入工件时省去空集合查询；有输入的任务仍逐项检查分支归属。直接调用 TaskService 的入口仍校验契约。原有 TaskService 保留幂等命令、任务代际、Outbox 和 Provider 账本语义。置 0 并重启可退回。 |
| `SELECT nf_hera_set_capacity_mode(true)` | 升级 0027 后，在 READ COMMITTED 的短管理事务中执行。数据库阻塞新的任务写入，从权威非终态任务回填全局、作品、身份和资源计数。激活后每次变更在同事务中增减，任何维度满额均整体回滚。普通角色无法从客户端设置配额。改为 false 恢复扫描策略；队列较浅时扫描仍是默认。 |
| `python -m scripts.workflow_projection suspend-maintenance` | 仅当全库没有配置任何分支投影时可暂停空触发器。任务表与计数表在一个事务中加锁，并加约束阻止误插投影。新管理命令 rebuild 会先恢复维护并从权威表回填。显式回退使用 resume-maintenance。 |
| 索引进程、SQL 形状复用、路由与弹性 | 索引进程拆分及连接池、FIFO、分片计数和异步投影均在隔离实验中对照。发布配置仍需按总连接数计算 API、索引及通知监听；此次复验没有启用新的全局默认。SQL 形状复用候选的源代码和回退提交保存在分支历史中。 |

数据库恢复保留 STALE 的任务代际，对仍可运行的任务按原规则推进代际并暂停。合法修复试图将终态任务重新激活、但准入容量已满时，写事务回滚并给出可重试的 BACKPRESSURE。模型 SENT / UNKNOWN 保留原有账本语义。

[根因与三版本测量](../../reports/performance/hera/ROOT_CAUSE_ANALYSIS.md) · [精确配额及恢复](../../reports/performance/hera/CAPACITY_COUNTERS.md) · [投影对照](../../reports/performance/hera/PROJECTION_CONTENTION.md) · [最终选择](../../reports/performance/hera/OPTIMIZATION_ABLATIONS.md)

进度管理操作要求 READ COMMITTED 事务。管理员须在取得任务表写入围栏后以新的语句快照回填；已建立快照的 REPEATABLE READ 或 SERIALIZABLE 管理事务会被拒绝，以免漏计并发提交。实时缩小索引配额采用非抢占方式。已取得的租约继续完成，最后一条超额租约结束后才允许新的领取。通知和调度变化的耗时见[租约、限额与公平领取复验](../../reports/performance/hera/SCHEDULER_RECHECK.md)。
