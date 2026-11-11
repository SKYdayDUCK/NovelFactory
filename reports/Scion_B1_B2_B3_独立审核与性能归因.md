# Scion 第二轮独立审核与性能归因

## 接管基线

2026-11-11 接管时 PR #2、#3、#4 的 head 依次为 a9547bd、23a6139、e757419，均为 open/draft。PR #4 依赖 #3 的 dev/p3-p4-continuity，#2 依赖 main 的 721f155。本轮开发使用原仓库的独立克隆。旧开发工作树干净，Git 全对象检查通过。旧恢复归档、数据库快照、运行记录和 182 行上传日志原地保留；日志最后一次已回读的 D 提交与 GitHub 实时 HEAD 相同，为 e757419aea6f00b9388fee14a92f4ea3b97444c0，tree 为 6530c20e34bc6569909d84f60c0142014e5b02d9。A0、B、C、D 的分支检查点全部可达。

[接管报告](Scion_中断恢复与开发接管.md) · [设计](增量世界版本与状态投影_技术设计.md) · [冻结等价性](新旧世界内核_等价性验证.md) · [原始配对](Scion思路_并发优化实验.md) · [原集成决定](并发优化_集成与PR审核.md) · [原性能剖析](NovelFactory_多Agent并发性能诊断.md)

## B1 调用路径

|生产调用|独立源码复核|已有测量与第二轮决定|
|---|---|---|
|DerivedWorldWorker 发布前复核|准备阶段用完整 revision 校验 snapshot 并生成派生物。发布事务锁定对应分支，按完整 WorldRef 复读 branch_seq、state_hash、manifest_hash，三个值与已准备内容逐一匹配，后续仍校验已有 artifact 和同一代发布。|200 人口夹具单条复核从 662894 JSON 字节降到 0。拆出仅覆盖此处的开关，并实测整个发布入口。|
|promote 枚举修订审核记录|候选版本的 compare、重放和推广复查在枚举外执行。枚举只消费 world_revision_id、provenance。后续继续复核批准及两侧 HEAD。|避免读取每个修订的快照，但历史缺少独立整次推广证据。独立开关，单独测量。|
|WorldQueryService._sample 固定 cut|query_revision 仍按 WorldRef 查权威行，读取完整 snapshot、state_hash、provenance、manifest_hash，执行 _checked_projection 及 schedule 校验。完整依赖请求与 Context 保留完整 Revision。|历史 warm STATE 延迟随实验条件波动；减少未用列不能消除主要 snapshot 校验和解码。普通查询继续默认关闭。|

[Kernel](../src/novel_factory/application/kernel.py) · [查询服务](../src/novel_factory/application/queries.py) · [派生工作者](../src/novel_factory/application/derived_worker.py)

## B2 的安全边界与收益归属

prepare 和 accept 的 _verify_tokens 都调用 _tokens。prepare 从注册模板在服务器生成 footprint，accept 在锁内重建并比对 reads、writes、negative_guards、constraint_groups 等。按 work_id、branch_id 和 reads∪writes 读取版本，再按原来的两个列表构建 token。缺行版本为 0，范围和负向守卫以模板声明的 scope 键代表。已注册资源账户及约束组覆盖共享资源；新增模板如漏登记依赖，稀疏查询和全量映射都会遗漏对该依赖的证书绑定。保持注册模板一致性用例。继续在原事务复查当前授权、task epoch、解释版本、审批和证书，不缓存这些决定。正式接受 bump 后仍收集完整 Scope 写 Revision。

在历史 536-scope 世界，11 个查找键返回 9 行；同输入整次 prepare 的 SQL 返回行 559→32，accept 1113→586；这里是应用返回行，非 PG 物理读。正常 200/500 Agent 分别有 80/200 次稀疏查询，纯读取工作负载为零。将其作为证书路径优化，第二轮检验调用延迟及整体业务结果。

[Scope 数据访问](../src/novel_factory/infrastructure/repository.py) · [模板](../src/novel_factory/application/templates.py) · [投影与 token 用例](../tests/queries/test_scion_projections.py)

## B3 的冷成本和零命中

propose 与锁内 accept 分别读取选中事件，在 evaluate 调用 _fallback_reason，检查解释/规则、完整 parent selection、身份、因果及严格时间。符合形状的冷请求仍对全部历史正文逐项 fingerprint 并排序，再因缺父证明调用完整 reducer，之后再为结果算摘要。证明位于每个 Kernel 的有界内存表，跨进程、重启、驱逐会丢失。历史首次提案在 1000 事件上 1593.78→2745.84 ms，连续接受 2589.28→898.30 ms。旧 HTTP 的普通 200 是 0/61（41 次缺已验证父版本、20 次时间不晚），500 混合是 0/151（101、50）。500 持续/1000 开放都是读，分母为零。并发服务运行同时间共享命令，改变 HEAD 不推导事件时间可安全追加。

候选改动应先判断是否值得计算证明，仅允许负向预筛提前回退。严格顺序、完整父状态、正文、规则、因果、撤销/证书检查均须保留。用相同数据库快照测量，区分跳过证明的冷操作、命中后缀和完整回退。保持实验开关，验证实际业务命中后再决定。

[证明实现](../src/novel_factory/application/replay_proofs.py) · [Reducer](../src/novel_factory/domain/world.py)

## 容量与工程 CI

基线完整 PG CI 652 项通过。基线 1000 Agent 开放读取计划 5000，HTTP 200/429/传输错误分别为 1165/3737/98；客户端实际 header 约 38.38/s，有效读约 8.73/s。500 Agent 的 600 秒持续读成功 11025/12000，余下 975 次为 429。200 过载也未通过。旧 PG Lock 采样不足以排除短锁，无法据此认定全局锁主导。以独立发压和服务进程核实鉴权、查询、控制面、事件环与连接池时间，继续使用原计划、完成分母和原容量门槛。

[基线工程运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42944951254) · [规模运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42944951227) · [200 Agent](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42944951307)
