# P6 Readiness Acceptance

本目录保存 P5.8-A 的阶段测试、运营者命令验证和现场运行模板。验收单位为“作品、分支、任务、代际、Attempt、不可变输入/输出哈希”。原始证据见 [raw](raw/)。工程状态、上游服务实际执行、费用和文学评价分别记录。

| 验收项 | 实际检查与结果 | 原始证据 |
|---|---|---|
| AR-01 同场景两种角色绑定 | 同一作品的两个角色任务冻结不同适配器，在真实 PostgreSQL 上通过两个本地 HTTP 端点并行发送；核对角色身份、上下文和单次预留。Codex 路径另以真实 app-server 二进制运行一回合，并与 DeepSeek 协议夹具并发。两个上游真实账户的并行仍为 NOT_EXECUTED。 | a3-multiwork-postgres.xml、a1_provider_protocols.xml |
| AR-02、AR-03 路由、预览、切换 | Workshop 管理员发布，NF 管理员在所属作品及分支采用；匹配优先级、冲突拒绝、代际冻结、旧配置不漂移、确认未发送时创建新代际。普通角色不取得路由目录或对方配置。 | a2-routing-postgres.xml、a2-workshop-routes.xml、a3-multiwork-postgres.xml |
| AR-04 发送结果未知 | 旧请求保留原预留和 Attempt；切换策略后新任务进入新端点，旧请求没有被重发。历史及新代际反例同时检查。 | a3-multiwork-postgres.xml |
| AR-05 协议与故障 | DeepSeek 的输出上限字段、JSON、空内容、结束原因和 429；Codex 一次性 Responses 传输、宿主异常退出、工具隔离、禁止重试和整次传输超时。 | a1_provider_protocols.xml、a1_transport_deadline.xml |
| AR-06、AR-07 多作品 | 用不同授权分别建立两个工作、不同预算和上下文；并发任务竞争相同账号、达到槽位后等待，释放后可继续；直接指定 profile 也受组织额度约束。原有调度器仍负责领取和恢复。 | a3-multiwork-postgres.xml、p6-operator-runbook.xml |
| AR-08、AR-09 跨卷 | V1 草稿与 V2 有界窗口并发；正式连续性产生前只保留候选。前提变化后使受影响节点失效，保留无关节点；重新签发局部修订后仍需显式移交正式规划。 | a4_pipeline_run.json、a4_pipeline.junit.xml |
| AR-10 提前审核 | 第一个可读章节完成后即绑定只读稿件，另一章节可仍处于写作中。角色意见保存在单独制品，合成审稿没有独立读者评分。 | a4_pipeline_run.json |
| AR-11 恢复与对账 | 两个 PG17 集群独立物理基础备份和 WAL 归档。分别重放错位点，验证 WORKSHOP 超前时冻结，NF 超前时只查询补齐；原 UNKNOWN、预留和 Attempt 身份不变。Redis/原 worker 的回归由既有测试单列。 | db01-independent-pitr.json、db01-independent-pitr.xml |
| AR-12 静态兼容 | 未启用路由策略的 P5.7 静态路径继续使用原配置；契约与原生用例按原断言验证。 | a2-routing-postgres.xml；全量 CI 以本次 PR 与 main 为准 |
| PERF-02、P57-CR-02 | 首轮角色的 29 个 Token 计入，未知金额保留 null；两阶段已知用量为 58。PLOT/STAGING 的未表态与明确审清在原生、顾问报告和编辑判定中分别处理。 | a0-postgresql.xml |
| Windows / WSL2 上线准备 | Linux 实测用 CLI 完成 Workshop 安装登记和 manager 授权、两本书 bootstrap、两套预算、双角色路由预览以及流水线登记。运行时使用两套独立 PG17 和 Linux 权限；Windows 宿主 / Docker Desktop 交互为 NOT_EXECUTED。 | p6-operator-runbook.xml |
| 实验数据 | 读取管理员指定的 Attempt，核对供应商响应与传输窗口，排除不同作品、无回执、同角色及夹具。人工持逐请求账单确认后可在同一账本幂等结算。 | p6-billing-and-overlap.xml |

## 支付和文学门禁

- 本轮新功能验证发送的真实生产推理请求数为 **0**。Codex 和 DeepSeek 两个线上账户的有效性、并行接收响应、实际用量与费用尚待现场授权。当前状态为 **NOT_EXECUTED**；没有将缺授权当作远端服务失败。
- 合成模型记录了协议请求、Token、预留与合成账单。真实已结算金额为 **null**，没有把预留、价卡估算、测试夹具或订阅费拆算为逐请求实际支出。
- 没有本次真实创作正文和独立读者原表。文学质量为 **NOT_EVALUATED**。
- 历史 Issue #13 / AT-30 的 50/200/1000 Agent 资格维持 **NOT_MET**。PERF-01 长员工历史分页继续开放。DB-01 的新证据限于本目录明确的两个物理集群和错位点。
- 在实验机上先运行无推理的预检，再按 README 执行本地小规模用例。拥有真实双账户回执后重新导出并行证据，取得独立读者评分后另作文学验收。

本次独立验收还从全新的数据库和对象目录重走了手册的管理者与双作品命令。专项组合测试及未配置环境的原始返回分别见 [P6 工程组合](raw/p6-focused-postgres.xml)、[离线配置检查](raw/offline-provider-preflight.json)、[真实执行状态](raw/readiness-status.json)。源码及镜像配置在 Linux 上验证，Docker Desktop 本身不在本次运行环境中。

集成期间 Linux NF 全量用例为 986 passed，另 17 个 subtests passed；该运行开始于最终审核修订前。双服务完整组合为 19 passed。单元作业中有一项 OAuth 签名用例原先意外依赖本机是否安装 Codex，修订后在没有 Codex 的 PATH 下也实际执行签名及声明校验。真实宿主测试在安装原生 CLI 的独立作业中仍然运行。最终合并按最新 SHA 的托管检查记录核对。
