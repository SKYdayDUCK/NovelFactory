# 原树和修复树的实际观察

原合并为 `1de471076b8fb4240f95ff8033c60db34c187404`，树为 `3fe25fcb17d2fd76618a0695b1d54641d71ce60f`。下列测试使用合成自有数据、已有生产服务入口和临时独立数据库。核验的是决定、权限与审计元数据。源材料和身份凭据留在受限测试进程中。主线成功工程作业与以下刻意在原树运行的红测是不同的证据。

| 问题 | 原树实际结果 | 修复后实际结果 |
| --- | --- | --- |
| P57-SEC-01 | 4 failed, 1 passed in 3.48s。[原始日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202876684/job/131130521377) | 5 passed in 3.06s。[修复日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202876684/job/131130521753) |
| P57-SEC-02 | 2 failed in 17.26s (DID NOT RAISE in both final decision checks)。[原始日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201932268/job/131127269538) | 2 passed in 26.80s。[修复日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201932268/job/131127269303) |
| P57-SEC-03 | 1 failed in 4.13s (original completed the valid native RETAIN)。[原始日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202380763/job/131128823797) | 11 passed in 39.94s。[修复日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324597/job/131132081080) |
| P57-CR-01 | 1 failed, 4 passed in 27.44s。[原始日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43199663974/job/131119434366) | 5 passed in 31.60s。[修复日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43199663974/job/131119434962) |
| P57-TC-01 | 9 failed, 5 passed in 154.96s。[原始日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896850/job/131113456793) | 14 passed in 134.55s (0:02:14)。[修复日志](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201789218/job/131126778921) |

## 回归条件与可验证断言

- **P57-SEC-01**　`tests/integration/test_harness_audit_scope.py` 使用普通准备输入、完成分派、无当前标签的历史阶段、静态对照，以及当前/历史任务绑定。两边读取同一份五项测试。既检验合法来源的访问，也以不匹配的合成分支核对拒绝。公开证据只保留策略结果。
- **P57-SEC-02**　在 `workshop/integration/test_dynamic_skill_dispatch.py` 和 `test_creative_review_independence.py` 中先正常生成结果，再经 Workshop 管理端撤销来源 Skill。规划与编辑控制器仍持有效 NF grant，W4 的独立审稿收据仍有效。两个原树测试均到达最终决定并报告 `DID NOT RAISE`。修复后都正常拒绝，原生计划指针没变，编辑决定没有写入。[两例同夹具前后对照](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201932268)
- **P57-SEC-03**　建立完整的原生编辑 session、卷级阅读与逐项保留理由。原树接受未单独登记的控制器，修复树验证实际 grant、epoch、读者、完整作者群和跨稿件实质身份。成对正例含已登记编辑、负例含漏登记或不匹配主体。另在两个服务中核验 Workshop 登记的额外贡献者。[完整旧决策](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202380763/job/131128823797) · [正常与拒绝控制](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203324597/job/131132081080)
- **P57-CR-01**　对同一真实 NF 合成留出稿分别签 PROSE 正例和 NAMING 保留负例，按真实 S5 入口冻结并执行两臂。原树把它判为 eligible，修复后报告 `OVERLAPPING_HELD_OUT_MANUSCRIPTS`。另用原实现生成两组旧格式冻结结果，再以修复模块核验，重叠记录的归档仍可回读、当前资格被拒，实质独立的旧正负稿继续通过。[旧版冻结报告重验](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43203462448/job/131132551642)
- **P57-TC-01**　原常规 Workshop 载体有 9/14 项因找不到 `aw-mcp` 或根解释器中的 Workshop 模块而失败。修复安装与 PATH 后，原集合 14/14 通过，并完成双服务身份与同一原生角色线程的 SSH 代理验证。审核新增测试后的主线 Workshop 结果另见 [主线回执](MAIN_MERGE_RECEIPT.md)。[原始红灯](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43197896850/job/131113456793) · [原套件复验](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201789218/job/131126778921)

## 未因中间失败放宽门槛

第一轮 S2 反例的故事时点早于初始前沿，测试未到达目标决定。将夹具调整为与冻结世界相同的时点后才取得有效红证。首个 SEC-02 修复也改变了批量独立性回执的返回次序，已恢复去重后的原请求顺序，并通过旧断言。首次编辑反例缺少完整原生决定，曾先收到 `EDITORIAL_DECISION_REQUIRED`，后续使用完整决定复现成功。编辑测试中的另一个授权时限和一个旧测试导入问题也已纠正。Workshop 第一次修复重放虽 14 项通过，最后却把显式检出的 SHA 与外层工作流 SHA 比较，签收失败，改为和实际检出 SHA 比较，完整重跑。[最初故事时点](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201631345) · [顺序回归](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43201626124) · [原有 13 项双库复核](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202624163/job/131129662707) · [初次编辑夹具](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202019314/job/131127566939) · [首个 Workshop 重放](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43199464368)

## 开放问题的证据等级

| 问题 | 已有的实际证据 | 尚未执行 |
| --- | --- | --- |
| PERF-02 | 原 main 在真实 PostgreSQL、原生隔离角色夹具和实际 S6 report 上跑两案。成功调用共 58 Token，29 被列为未归类，单次 UNKNOWN 已返回 29 Token，报表记录 0 已知 Token，金额为 null。原生账本保留 Attempt。[两次真实报表查询](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43202106191/job/131127864815) | 生产流量下的分组修正与历史报表回填 |
| DB-01 | 配对恢复实际运行，一份物理基备、一条 WAL 时间线，下辖两个业务库。独立双服务运行另行核对系统标识。物理 PITR 对 71 表、39 对象及保留的 UNKNOWN 作了回读。 | 两个独立归档集群在不同恢复点的物理恢复 |
| PERF-01 | 静态追踪 5 个事件和 2 个修订的循环，得到 7 个逐项 SQL 与 21 次 fsync 调用 | 长历史 P95、锁等待及峰值 |
| P57-CR-02 | 两树上的普通独立读者各取得完整阅读回执，PLOT/STAGING 留空 observations 均返回 native PASS 与工程 advisory ACCEPT_CANDIDATE。每个版本 2 项通过。[原树与修复树证明](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43205583624) | 明示审清或未给判断的产品语义、人类读者验收 |

原 S6 与最终主线修复代码的两份原始输出分别为同一场景的一个校准批，均有 A/B/C，三个实验窗口、九次 NF 合成 Attempt，独立确认批为零。[S6 原始 JSON](../../harness/evidence/s6/three-arm-comparison.json) 与 [最终代码原始 JSON](../../harness/evidence/s6/post-review-three-arm-comparison.json) 均保存完整逐组输出。真实商业模型和现场文学效果没有实测记录。
