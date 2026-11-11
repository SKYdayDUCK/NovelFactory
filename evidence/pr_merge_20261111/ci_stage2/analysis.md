# PR3 008fccf 实际 CI 只读核验

## 结论

PR3第一集成检查点 `008fccf23291c6f74693e450a88c224a436be7f6` 的两次工程回归均 **595 tests / 0 failures / 0 errors / 0 skipped**，独立unit子集344项通过。两次真实隔离PITR均通过。500Agent正常业务在PR和push运行都达到 **51/51新正式提交，首试失败0，容量/业务恢复/正确性门槛PASS**。1000Agent开环和600秒探针在两次运行均NOT_MET，因此不具备以这些规模结果宣布全面容量达标或合并的条件。

八个run全部完成，18个Artifact ZIP均下载并与GitHub提供的SHA-256逐一匹配，18个job日志已保存。全部运行纳入索引，没有重跑、选择最佳记录或用旧HEAD代替新HEAD。

## 代码与证据绑定

PR实际checkout为 `4e1b2b0f73950d72740f2643d660b2d35a94a57c`。通过GitHub Git Commit接口读取证明其与008fccf的tree相同：`350a3bd277ed1405e6e2ff4333315c4707c25eff`。合并测试ref的父节点为0a0af490与008fccf；这证明本次PR测的是在PR2基分支上的P3增量，不表示main已改变。记录在 checkout_tree_proof.json。

| 类别 | PR run | push run |
| --- | --- | --- |
| 工程 | [42856323577](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323577) SUCCESS | [42856309898](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309898) SUCCESS |
| PITR | [42856323203](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323203) SUCCESS | [42856309403](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309403) SUCCESS |
| 规模 | [42856323313](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323313) FAILURE | [42856309344](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309344) FAILURE |
| 独立200与配对 | [42856323239](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856323239) FAILURE | [42856309340](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42856309340) FAILURE |

## 业务完成、有效请求和容量分别报告

500Agent正常：两次固定2851逻辑请求全部发出，2802个200、49个预期竞争409。新正式效果与revision均51，shared及independent资源均99，回执重复未计入新增提交。PR首试P95/P99为247.10/448.32ms；push为713.30/876.05ms。扣除重复回执后的全测量阶段有效调用率分别7.8743/7.8513每秒；这是原10/s到达业务实验，不能把它当1000RPS实验通过。

1000Agent：固定5000个唯一请求索引全部保留，且5000都有header-send-start记录；无重试。PR有效1339/5000，3572个429、89个ReadError，wire43.45/s；push有效1129/5000，3793个429、78个ReadError，wire37.86/s。首试P95/P99分别8514.99/10911.05ms和8954.13/11461.65ms。所有有效响应经完整Schema和精确WorldRef/状态校验，正确性错误列表为空；只读探针确认HEAD未变，没有正式世界提交。原wire目标1000/s、至少950/s门槛及全部有效成功条件均未满足。

该开环脚本已在网络测量结束后做响应校验，PR额外校验2.63s、push3.67s；不能再把网络窗口中的响应Schema校验当作唯一原因。生成器dispatch也仅43.45/37.92每秒，header/dispatch相近；服务同时产生大量429及ReadError。当前结果证明这套共享机器上的生成器/服务组合失败，没有证据可宣称服务单独达到1000RPS，也未测得客户端CPU分阶段成本来唯一归因调度器。

独立200正常两次21/21、首试0；热点两次也21/21且业务恢复/正确性PASS，但PR首试585个失败，push647个失败，容量NOT_MET。全部失败尝试与预算耗尽记录在index.json；业务恢复不等于所有只读请求有效成功。

## 600秒真实探针

固定500个Agent、20/s、600秒、12000个唯一索引；两次都12000条完整records，没有client_drop或transport_error。

| 指标 | PR | push |
| --- | --- | --- |
| 有效成功 | 11156/12000 | 11942/12000 |
| 429 | 844 | 58 |
| 实际call start/s | 20.00056 | 20.00051 |
| 有效查询/s | 18.52765 | 19.90005 |
| 实际时长 | 602.127s | 600.099s |
| P95/P99 | 2334.40/2382.52ms | 1487.19/2142.94ms |
| CPU型号 | EPYC7763 | EPYC9V74 |
| API总CPU秒 | 1140.78 | 912.88 |
| probe gate | NOT_MET | NOT_MET |
| long duration gate | NOT_EXECUTED | NOT_EXECUTED |

这次发送率本身已达到约定目标，失败位置可明确定位：所有844/58个429分别等于query/world admission的timed_out计数。认证与ingress均完成12000次，认证基本无积压；query累计等待18306.42/1729.99秒（多请求之和），峰值队列44/38（保守进程峰值求和）。因此不能把这个600秒失败仅归于客户端生成不出负载。

两次总API池均8、每个进程4，峰值8、结束0；SQL失败0，最大应用事务0.279/0.227s，最大SQL cursor0.159/0.126s。600秒样本60组均保留。该规模workflow未启用NF_BENCH_DIAGNOSTICS，没有函数级CPU时间，不能只根据总CPU将瓶颈确定为单一函数；200诊断中的版本快照/JSON成本是进一步验证的优化假设。600秒成功与否均不能替代多小时生产稳定性验收。

## 派生工作者：已证明和未测范围

实际工程JUnit中，tests.integration.test_derived_worker共10项两次全部通过：四个真实进程退出点恢复、乱序不回退watermark、重复消费者与过期lease、分支隔离、迟来artifact失效不改固定index身份、work-before-branch锁顺序，以及worker延迟时同步scope仍保护接受。PITR另验证旧任务worker被fence、未知调用和预留金额仍保留；不能把该任务fence与所有派生负载目标混为一项。

本次500/1000/600秒脚本仅启动API服务进程，没有同时启动DerivedWorldWorker。scale使用两API进程、每进程pool4、总8；独立200用一API进程总pool8并开启诊断。API admission每个scale进程2，总4，不能把保守峰值求和当精确同时峰值。

派生worker CLI目前是串行处理，默认100条（允许1..10000）、默认lease30秒，Database配置pool上限5。它的顺序事务设计峰值为1个连接，配置上限5本身不是实测使用了5；仍应把独立pool纳入部署预算。尚无API加派生worker并行的500/1000负载、积压恢复速率、持续CPU/内存资源预算实验，不宣称这些已验收。该边界已与PR3审计agent交叉核对。

## PITR与Artifact完整性

两次均实际PostgreSQL17.11、fsync/synchronous_commit/data_checksums开启；pg_basebackup和pg_verifybackup exit0、归档4段无失败，目标提交丢失0、故意排除后续提交1，37张表和19个对象对应的目标状态核验通过、原commit receipt相等、旧worker拒绝。PR恢复到已验证服务2.813s，push0.486s，都是小型隔离fixture，不能称生产RTO/RPO保证。

发现一项真实证据保留缺口：内部SHA256.json列11项，workflow上传仅json/log，实际缺archive_segment.py、restore.conf、source.conf三个生成文件。18个ZIP的外层digest无误；PITR已保留8项的内部hash全部匹配。不能声称整个11项清单可恢复。已报告root，后续PR3检查点将补上传；此处原始结果不修改、不伪造补齐。

## 文件索引

- summary.md：全部六次混合业务、两次1000、两次600秒和JUnit/PITR表。
- overview.json：简洁机器索引，含run/job状态、artifact id/digest、实际数值和边界。
- index.json：详细服务pool/admission/CPU、原始请求复算、worker JUnit案例等。
- final_metadata.json：GitHub最后读取的八个run与18个job/Artifact元数据。
- 各run/artifact目录：完整原ZIP、逐项digest记录和解包证据；各run/jobs目录：真实job日志。
