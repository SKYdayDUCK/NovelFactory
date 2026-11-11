# NovelFactory 多 Agent 并发性能诊断

日期：2026-11-11（北京时间）。实验分支基于 `23a61398278c9a13e6450d444d4a6d25a88ef5e0`；本报告先记录已核验的历史诊断及新一轮测量方法，新增数据只在实际运行后补入。原容量条件保持不变。

## 1. 当前可确认的瓶颈结论

现有正式提交机制仍保证单一权威世界。最直接的优化目标是完整 Revision JSONB 的重复读取/校验，以及局部提案和正式接受对全部选中历史的重复重放。当前证据不支持把问题统称为“数据库慢”或“全局锁竞争”。独立客户端校准也证实旧单进程发送器自身无法达到既定1000 RPS，所以后续必须同时报告实际到达和有效业务吞吐。

### 最新原分支的实际结果

| 场景 | 最新实测 | 解释 |
|---|---|---|
| 200 普通，最终 PR3 | 3次均21/21正式效果，首试失败0 | 正常业务恢复已不是当前主要阻断。 |
| 200 热点，最终 PR3 | 3次均21/21；首试失败574/633/653，最终耗尽读69/112/127 | 业务恢复不等于全请求成功，热点容量仍失败。 |
| 500 混合，最终 PR3 | 51/51效果，2851首试；有效7.8756调用/s | 原10/s混合负载通过，不能写成1000 RPS通过。 |
| 1000 只读，最终 PR3 | 1528/5000有效，3419次429、53 ReadError；header48.068/s、有效14.314/s | 原5000全有效且至少950/s条件未满足。 |
| 500 Agent，600秒20/s | 最新12000/12000通过；同代码此前11708/12000，有292次429 | 最新单次通过不能证明重复稳定或多小时容量。 |

200来源：[最终四格42864580621](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864580621)、[独立200 42864580618](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864580618)。500/1000/600秒来源：[最终规模42864579931](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42864579931)。历史失败来源：[此前规模42859202367](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202367)。这些是继承且重新核对原始归档的结果，不是本轮候选的A/B。

## 2. 真实调用路径与代码证据

| 阶段 | 真实路径 | 当前成本和语义边界 |
|---|---|---|
| 鉴权 | HTTP → Kernel.authenticate → grants | 当前grant仍逐次读取；任何缓存优化不能记住已撤销授权。 |
| 固定cut读取 | WorldQueryService.query → _sample → _task/_selector → Kernel.revision → repo.one(revisions) | 全行JSONB返回，完整snapshot fingerprint，schedule校验；结果cache查找发生在_sample之后。 |
| Context | context_compile → _sample → _execute/_state → 构建facts/dependency → contexts/dependencies | 读相同权威Revision，再生成当前任务/代际的上下文；不能跨身份复用授权结果。 |
| 候选 | Kernel.propose → revision → 全selection事件查询 → templates.build → replay → 候选objects/DB | 普通局部动作也读全历史并重放；候选持久化取得branch update锁，但不推进HEAD。 |
| 证书 | prepare → current revision → ancestor → build → _tokens → Certificate | 祖先已改成metadata递归CTE；scope_map仍读完整分支scope集合。 |
| 正式接受 | accept → _accept_transaction → auth/approval/task/cert/scope → branch lock → current → 全事件读取/replay → revisions/revision_events → HEAD CAS/receipt/outbox | 原子接受保持正确；全历史处理及完整投影写入都在分支锁事务中。 |
| Retcon | REPAIR_EVENT → _revalidate_repair → 前缀重放 → promote | 验证后继命令与因果，当前200事件repair预算和2000同步历史预算均是保留边界。 |
| 派生索引 | outbox → DerivedWorldWorker → generation/watermarks | 属可重建派生结果，不能替代未更新的权威状态；API与worker共同容量未测。 |

基线文件位置：`src/novel_factory/application/kernel.py` 的 `revision/propose/prepare/_tokens/_accept_transaction`；`application/queries.py` 的 `_sample/query/context_compile`；`infrastructure/repository.py` 的 `many/scope_map/ancestor`；`domain/world.py` 的 `replay/_apply_group/_check_cardinality`。

`replay` 不只是应用新事件：每次都重新验证全部事件Schema和canonical类型、构建时态顺序、重放effects，然后对每个关系时间边界验证全量关系基数。该结构有历史数×关系数增长的风险；本轮要用增长曲线和cProfile区分实际主导项，不能只凭代码推出一个吞吐数字。

`revision_events` 当前在每个新Revision写入全部选中事件，因此单次写入和累计存储也随历史增长。两个有限优化即使消除重放，仍不能宣称已把整个正式提交降为O(变化量)：完整snapshot/selection/scope_versions写入、完整state_hash与每版本事件选择仍可能保留。

## 3. 已有服务、客户端、SQL和锁证据

最终PR3独立200热点累计线程CPU：query58.86秒、sample45.42秒、revision35.00秒、contract15.26秒、JSON decode13.71秒、Context8.73秒。这是嵌套计时，不能相加。客户端CPU41.78秒，其中响应校验5.82秒；API CPU89.21秒。HTTPX连接池等待P95/P99为140.36/363.29ms。另两次矩阵在不同CPU型号，不能合并为严格同环境对照。

该次1196个429全部对账：鉴权拒绝967/超时4，查询拒绝96/超时129，ingress0。两个矩阵重复则存在少量ingress拒绝，所以不能只解释一个准入位置。三次均PG总池8、峰值8、结束0、SQL错误0；最大应用事务约0.594/0.366/0.460秒，cursor最大0.184/0.152/0.149秒。

第一轮940个PG活动采样没有Lock类等待，主要ClientRead，少量WalSync/WalWrite。这不能证明没有短锁；也不支持把全部背压归因数据库行锁。旧SQL cursor计时不含结果JSON解码，连接获取含pool/pre-ping/connect，旧事务计时止于commit回调前，各自边界不能混用。

固定回包发送器校准四次header197–241/s，客户端接近单核满载，独立服务只约6–8%单核；真实服务仍有大量429。这说明客户端负载能力和服务容量是两个同时存在的问题。来源：既有 `evidence/pr_merge_20261111/client_calibration` 与最终head原始诊断归档。

## 4. 新一轮分阶段剖析方法

脚本：`benchmarks/scion_profile.py`。它使用同一真实PostgreSQL17隔离数据库与同一连接预算，所有世界、历史事件、任务、上下文、提案、证书和正式接受均经现有Kernel服务创建，不直接填充投影或事件表。

测量分为两个互补数据集：

1. `make_population(200)`：复用现有并发实验相同世界构造与真实grant/task，4个已接受种子事件，用固定WorldRef查询冷/暖结果cache，并测完整DEPENDENCY_GUARDED资源提案—证书—接受。
2. 历史增长世界：固定实体规模，通过真实SET_RELATION接受将历史增长到1/20/100等明确检查点；每点查询固定cut并测STRICT_HEAD完整提交流程。profile样本另做一次新正式提交，因此其历史数明确为前一普通样本+1；不伪称完全相同世界的配对。

每条操作保留：wall/processCPU/RSS、SQL数量/类型/涉及表/cursor执行时间与失败数、psycopg JSON编码和解码字节/时间、replay调用次数和事件数、包含计时阶段、完整UOW含连接获取及commit/rollback返回的时间。JSON codec字节是驱动收到/送出的JSON值长度，**不包含标量字段、协议开销和物理I/O，不能称全部网络读取字节**。

cProfile样本明确标记，剖析自身有开销，不与普通样本混算中位延迟。所有重复查询比较完整payload、evidence、knowledgecut和WorldRef；只在验证UUID/时区时间格式后剔除每次生成的request_id/read_cut_id/resolved_at。正式接受验证父版本、事件选择数量、HEAD和幂等回执，重复回执计0个新世界提交。

记录实际导入模块的`__file__`及SHA-256，并要求来自被测工作树。这样可用候选venv运行冻结baseline源码，避免editable安装悄悄把baseline实验导向候选代码。SQL参数、访问token、证书秘密和完整数据库URL不写出。

### 执行命令

在冻结baseline工作树运行；`PYTHONPATH`同时固定baseline/src与baseline根。以下是首轮1/20事件和200人口采样，输出位置明确属于本轮证据：

```bash
PYTHONPATH=/workspace/scratch/3dd241864a32/baseline/src:/workspace/scratch/3dd241864a32/baseline \
/workspace/scratch/3dd241864a32/NovelFactory/.venv/bin/python scripts/with_postgres.py \
  --pg-bin /workspace/scratch/cf3ee8d0c6c4/pg17/dist/usr/lib/postgresql/17/bin \
  --log-dir /workspace/scratch/3dd241864a32/NovelFactory/evidence/scion_20261111/phase_a/profile_v2_pg \
  -- /workspace/scratch/3dd241864a32/NovelFactory/.venv/bin/python -m benchmarks.scion_profile \
  --agents 200 --entities 200 --histories 1 20 --repeats 3 \
  --output /workspace/scratch/3dd241864a32/NovelFactory/evidence/scion_20261111/phase_a/profile_v2.json
```

若本地PG二进制依赖独立动态库，需要使用该PG包已有运行环境；不得因启动失败改用SQLite。`with_postgres.py`不用`--migrate`，脚本会在独立数据库迁移。

## 5. 本轮新测量状态

首个真实PostgreSQL17启动/语义smoke已完成：10人口、20实体、1事件，14条操作、2个数据集，固定cut完整响应及正式接受/幂等核对通过，原始记录为`evidence/scion_20261111/phase_a/smoke.json`。实际导入模块来自冻结baseline工作树。此次发现psycopg每次checkout创建闭包adapter的诊断警告；已改为每轮预建两个稳定callable并用weakref避免保留整轮meter，因此smoke只作为脚本启动/语义证据，不作为最终性能对照。

200人口及1/20事件正式归因已完成，**45条操作、3个数据集，PROFILE_COMPLETE_WITH_INVARIANTS_CHECKED**，原始数据为`evidence/scion_20261111/phase_a/profile_v2.json`。PostgreSQL17.11、shared_buffers128MB、fsync与synchronous_commit均开启、max_connections60；应用池8与观测池1，共享宿主可见CPU0–8。所有操作都在冻结`23a6139`应用源码上执行，导入文件及hash逐项保存。

### 5.1 仪表自身导致的失败测量

首轮`profile.json`虽然所有语义检查通过，但cProfile发现每次checkout重复执行`set_json_dumps`会基于已经定制的dumper再创建子类，造成类型继承链随fixture增长。该轮暖查询的60–103ms落在`_make_dumper/type.__new__`上。因此它标为 **INVALID_FOR_LATENCY_INSTRUMENTATION**，保留原始JSON和排除原因，不用于应用延迟基线。第二轮改为每个物理连接仅注册一次稳定codec，完整重跑相同参数。三份原始文件及SHA、处置状态见`measurement_disposition.json`。这是修正仪表污染，不能将108ms→29.6ms写成NovelFactory优化收益。

### 5.2 有效基线：相同200人口世界的阶段成本

该fixture有262实体、619关系、4个已接受事件、536个scope；单个snapshot的JSON文本615,159字节，PG列存储117,535字节。下面普通查询暖样本是3次中位，其他阶段各1次普通样本；cProfile样本另列、未混算。JSON字节单位为驱动实际解码字节，包含阶段内所有JSON列读取。

| 阶段 | wall ms | CPU ms | SQL条数 | 解码JSON字节 | 完整重放事件 |
|---|---:|---:|---:|---:|---:|
| 结果cache冷 STATE | 33.899 | 27.521 | 20 | 669,046 | 0 |
| 结果cache暖 STATE | 29.627 | 23.058 | 20 | 669,046 | 0 |
| Context Compiler | 49.608 | 40.571 | 47 | 676,840 | 0 |
| 提案 | 85.867 | 73.927 | 56 | 1,232,942 | 5 |
| 准备证书 | 47.742 | 39.514 | 35 | 684,065 | 0 |
| 正式接受 | 135.199 | 110.058 | 61 | 1,261,753 | 5 |
| 同一接受请求重试 | 2.549 | 1.784 | 3 | 2,398 | 0 |

三个暖查询仍各20条SQL、669,046 JSON字节，证实结果cache未省掉Revision加载。选中暖样本repeat2的revision阶段约16.24ms，整个_sample约22.85ms；它们互相包含，不能求和。提案中完整replay约29.17ms、JSON解码11.01ms、SQL cursor15.71ms；正式接受中replay30.22ms、JSON解码11.65ms、SQL cursor34.49ms。最终写UOW（连接获取到commit返回）约125.76ms，包含完整应用CPU与SQL，并不是125.76ms锁等待。正式接受序列化12个JSON值、670,888字节，编码约2.73ms；省掉历史重放也不会自动省掉完整Revision写入。

### 5.3 历史增长世界

另一固定201实体世界，用真实SET_RELATION接受得到1和20事件检查点；关系数201→220，snapshot文本236,019→258,231字节，完整历史选择关联表行数1→210。1+2+…+20=210与现有逐版本完整选中集合存储一致，不是一次20事件提交写210行。

| 历史检查点 / 阶段 | wall ms | CPU ms | SQL条数 | 解码JSON字节 | 完整重放事件 |
|---|---:|---:|---:|---:|---:|
| 1 / 暖STATE，中位3次 | 20.183 | 15.887 | 20 | 278,843 | 0 |
| 20 / 暖STATE，中位3次 | 21.976 | 16.877 | 20 | 304,223 | 0 |
| 1 / 提案 | 52.794 | 43.833 | 52 | 487,366 | 2 |
| 20 / 提案 | 123.855 | 110.739 | 52 | 545,343 | 21 |
| 1 / 正式接受 | 82.539 | 65.779 | 59 | 509,913 | 2 |
| 20 / 正式接受 | 143.484 | 121.655 | 59 | 567,898 | 21 |

SQL条数未随历史增加，但返回JSON、解码和重放成本上升。检查点间存在setup和宿主噪声、每次写仅1样本，所以这是归因观测，不能拟合可靠渐近指数或宣称稳定百分比收益。21事件前缀后另一次profile提案（输入22事件）中replay累计0.217s，validate_contract累计0.145s，_check_cardinality累计0.054s且调用45次、temporal.contains调用19,518次。cProfile有明显额外成本，数字只用来定位调用结构。

### 5.4 SQL、等待与未测项

观测器采样96次，未记录观测错误；累计连接样本包括idle/ClientRead16、active无等待7、idle-in-transaction/ClientRead59、WalSync11等，没有Lock类。采样不能排除短锁，且本轮顺序调用没有并发锁压力。观测总WAL为8,083,392字节，包含fixture构造和全部测量，不能除以某一个接受请求当作提交WAL成本。

本轮证据支持优先试验：验证过的不可变Revision复用以减少JSON/全hash成本；严格后缀情况下的纯增量replay；仅读取原footprint需要的scope版本。三项均需保留旧路径并通过同输入差分。没有证据要求引入外部缓存或第二套权威存储。100/500/1000历史及实际多Agent A/B由下一检查点继续，尚未通过的容量不改判。

HTTP到达、客户端pool排队、服务器队列、429/503、独立提交TPS和完整业务完成率需要保留原200/500/1000与持续负载脚本的服务实验。该剖析脚本`capacity_gate=NOT_APPLICABLE`，不能用函数加速代替容量验收。
