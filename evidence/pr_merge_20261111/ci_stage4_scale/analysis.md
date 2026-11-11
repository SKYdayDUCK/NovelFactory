# Stage4 容量与恢复独立审核

本记录覆盖 `e3c7b79b0058656f11257ea55edc75371382b73e`，四个指定 run 已全部终态。8 份 ZIP 均与 GitHub digest 相同，9 个实际 job（含 PITR 失败 attempt1）checkout 的 Git tree 均为 `72328df65eafff795a5df1a10117c68c2d899bb6`。所有 39,702 条负载尝试保留。

## 各场结果及计量口径

| 触发 / 场景 | 业务或有效完成 | P95 / P99 ms | call-start / header-start 每秒 | goodput 每秒 | 原门槛 |
|---|---:|---:|---:|---:|---|
| push / 500-mixed | 51/51 新提交 | 390.0694 / 621.8260 | 8.1543 / 8.1554 | 7.8696 | PASS |
| pull_request / 500-mixed | 51/51 新提交 | 241.0542 / 394.0898 | 8.1609 / 8.1618 | 7.8765 | PASS |
| push / 1000-read-open | 1181/5000 | 8761.0905 / 11267.7594 | 38.2481 / 38.1618 | 8.8187 | NOT_MET |
| pull_request / 1000-read-open | 1203/5000 | 8406.3466 / 11360.3971 | 38.5633 / 38.4903 | 9.0483 | NOT_MET |
| push / 500-read-soak-600s | 11708/12000 | 2275.5909 / 2326.5767 | 20.0010 / 未记录 | 19.4477 | NOT_MET |
| pull_request / 500-read-soak-600s | 12000/12000 | 446.5278 / 589.4043 | 20.0009 / 未记录 | 19.9974 | PASS |

500 混合场景延迟是全部首试（含约定的 409 冲突），goodput 扣除重复回执。call/header 速率跨全部阶段，包含写入限速和阶段间隔；该场景调度参数为10调用/秒、写入阶段至多1/秒，没有执行1000 RPS开放到达部分，open-loop gate为NOT_APPLICABLE。两次各有102条回执，只有51个唯一command/revision，重复回执逐项相同；49个409均为预期DEPENDENCY_CONFLICT，首试操作失败及重试耗尽均为0。

1000 开放只读的延迟覆盖全部5000尝试，包括429及ReadError；goodput只含schema与状态内容验证成功的响应。header-start是HTTPcore事件，既不是抓包也不是服务端接受证明。调度至完成延迟另存原数据。

600秒探针延迟覆盖全部12000实际发送尝试，无client_drop。原探针仅记录call-start，没有header-start测量，表中不以推算数填充。PR为12000/12000 PASS；push为11708/12000、292个429，NOT_MET。两次long_duration_gate均为NOT_EXECUTED，不能作为多小时生产验收。

## 429 位置及失败保留

- Run 42859202367 / p3-open-arrival-1: HTTP429=3731，鉴权拒绝=1491，查询队列拒绝=426，查询等待超时=1814，入口拒绝=0；服务计数之和与429完全一致。transport=88，correctness_error=0。
- Run 42859219338 / p3-open-arrival-1: HTTP429=3706，鉴权拒绝=1401，查询队列拒绝=555，查询等待超时=1750，入口拒绝=0；服务计数之和与429完全一致。transport=91，correctness_error=0。
- Run 42859202367 / p3-stability-1: HTTP429=292，鉴权拒绝=0，查询队列拒绝=0，查询等待超时=292，入口拒绝=0；服务计数之和与429完全一致。transport=0，correctness_error=0。
- Run 42859219338 / p3-stability-1: HTTP429=0，鉴权拒绝=0，查询队列拒绝=0，查询等待超时=0，入口拒绝=0；服务计数之和与429完全一致。transport=0，correctness_error=0。

1000 两次均存在显著服务端背压，不能全部归因于负载生成器。独立生成器校准是另一种合成传输证据，不能将其HTTP 200算入此处的业务或容量成功。push探针的292次429分布为graph12、history18、state262，均为查询准入超时。全部非有效/错误记录独立保存，索引共8006行，其中98行是混合负载预期409，其余是开放/持续探针的真实失败。

两个600秒job均记录4逻辑CPU、AMD EPYC 7763，源码树亦相同，但共享runner分配和运行环境并不相同。当前证据显示运行差异，无法仅凭CPU型号断定原因，也无法将较好的一次替代失败的一次。所有读探针最终连接占用为0、聚合API连接池8；没有运行API与派生worker并发容量实验。

## PITR 与一次环境失败

PR run42859219591 attempt1在下载PostgreSQL签名公钥时curl(56)失败，应用安装和恢复未执行；未创建ci-evidence导致后续manifest/upload继发失败且没有artifact。完整原日志和首次元数据保留。root只通过连接器重试该失败job一次，源码及门槛未改；attempt2单独记录。

- Run 42859202666 / artifact 13785125246: PASS；11/11 manifest成员实存且SHA一致，ZIP另含manifest自身，共12文件。目标提交丢失=0，旧worker拒绝=True，原正式回执一致=True，隔离小fixture恢复=0.455949s。
- Run 42859219591 / artifact 13784955992: PASS；11/11 manifest成员实存且SHA一致，ZIP另含manifest自身，共12文件。目标提交丢失=0，旧worker拒绝=True，原正式回执一致=True，隔离小fixture恢复=0.486474s。

已上传内容仅为生成的配置、归档脚本、校验、日志和结果；没有PostgreSQL数据目录、物理备份或WAL段。小fixture恢复测量不代表生产RTO或远程存储容灾。

## 合并解释

500混合工程与业务完成已通过这两次固定输入运行。1000原容量目标两次仍NOT_MET；600秒稳定性一过一未过，不能宣布稳定容量。真实模型调用为0，多小时与生产规模实验未执行。此记录不批准降低门槛或合并未通过的范围。

混合负载git_dirty=true包含运行前写出的源码快照；已逐项核对快照hash和实际checkout tree，没有宣称整个工作目录在测量时无生成文件。完整原始状态、全部失败行及校验清单见相邻summary.json、analysis_index.json.gz、index.json。
