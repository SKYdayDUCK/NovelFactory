# 已知失败

- AT-30，issue #13 开放。旧 50/200 热点和 1000 开环读存在原门槛未达到的问题；[指标、基线和历次失败样本](02_容量与实验原始证据.md)。
- P5.5 500 Agent、600 秒、20 RPS 的旧候选曾出现 492 与 46 次 HTTP 429；最终候选云端原条件 12,000/12,000，另一共享主机的同业务源码候选与 P5 对照均未达到原线。保留尾延迟、限流和 7 次传输异常的原始数据。[候选及合并后实测](p55-main/README.md)
- 冻结阶段的在途撤权、高风险审核及双库错位修复分散于后续提交，见 [Workshop 证据](01_Workshop及桥接证据.md)。在相关阶段合并前验证各项。
- 付费创作与人工文学验收尚无本轮授权和实际结果。合成路径按 fixture 登记。

- W1 旧候选在带延迟外键事件的历史事务中迁移失败（SQLSTATE 55006），`ac5962e0` 修复；最终 `48c20a10` 的同一失败用例及旧发行服务升级均在 PostgreSQL 17.11 上通过。[原故障和修复回执](evidence/W1/candidate-48c20a1/workshop-ci.json)

- W2 早期候选 `772ee36f` 的 PEL 测试错误地假定全库随机顺序前两项均可采纳；终态使用两笔真实授权事件，仍全量重放并覆盖拒绝和去重。[原失败](evidence/W2/candidate-162af26/previous-pel-failure.log) · [修复及主线验收](W2_主线签收.md)

- NF Bridge 原实现允许已过期安装触及新预留或发送。相同数据库和发送前拦截协议下，旧实现失败两项，修复后通过两项。此前 UNKNOWN 的原始费用仍可对账。[对照证据](evidence/NF-Bridge/candidate-26bf033/expiry-control.zip)

- 桥接初版 `26bf0337` 的托管 PG 套件有 1 项失败、805 项通过、17 项跳过。历史 S9 用例的目标迁移写成 head，导致以 0016 断言查验 0017。新版本将历史边界固定在 0016，另验从发布库升级到 0017、非空降级拒绝和原 67 张表内容。[失败与修复原件](evidence/NF-Bridge/review-26bf033/upgrade-controls.zip)

- NF Bridge 已修复出站权限变化、跨安装操作、旧升级端点及 S9 等待进度。已用独立本地实测覆盖托管跳过的 17 项 Linux 角色约束，四项 AT-30 原门槛仍保留。[主线签收](NF_Bridge_主线签收.md)


W3 主线验收。原四项 AT-30 继续在 #13 跟踪，未涉及付费模型创作。[签收](W3_主线签收.md)。

- W4 公开候选的首次完整 NF 运行 831 项通过、3 项失败；测试用 `nf_fixture` 在建立受控损坏样本时缺少 PostgreSQL 参数 `session_replication_role` 的 SET 权限。只对一次性 fixture 身份补充此权后，4 项定点复跑和全部 834 项均通过，两轮全量节点集合一致。首轮日志/原 XML 各脱敏三处实际口令，其余原件字节与执行结果一致。[NF 全量原件及三次运行](https://github.com/SKYdayDUCK/NovelFactory/blob/5f07125458a0249c3203c9c0d5b03ff92abdcd50/reports/integration/evidence/W4/candidate-3ed83ff/independent/local-nf-full.md)。
- 2026-11-11 21:02:59 UTC，GitHub Actions 组件为 `major_outage`。W4 两条正式 CI 从 20:28:33 UTC 起排队，尚无被分配的作业，两条运行也没有待部署审批。发布流程保留原未合并状态。历史 AT-30 仍沿用 Issue #13。[GitHub 平台事件](https://www.githubstatus.com/incidents/dvy0t3mvh1lg) · [该时点的 GitHub 状态、PR、main、作业与部署审批回执](evidence/W4/candidate-3ed83ff/platform-outage-20261111T210259Z/checkpoint.json)。

- Actions 官方事件结案后，W4 旧 `3ed83ff` 两项运行在 21:26:37 UTC 仍各有零个作业。无产品变化的普通快进 `1cc12ca` 已获得新的独立运行编号；同树旧 NF 834/834、本机 Docker 跳过以及原平台事故均各自保留原证据。[正式运行、合并模拟树和父提交](evidence/W4/candidate-1cc12ca/independent/candidate-checkpoint.json) · [前一候选 834/834 原件](https://github.com/SKYdayDUCK/NovelFactory/blob/5f07125458a0249c3203c9c0d5b03ff92abdcd50/reports/integration/evidence/W4/candidate-3ed83ff/independent/local-nf-full.md)。

- W4 已在真实主线 `6c711adf794842bdf4469ab51e89093d37761bcc` 签收。平台事故、旧 w004b 缺失视图以及测试 fixture 参数权限的原试次均保留。两项主线 CI 成功，原 17 项托管宿主限制由同 SHA 原生复验覆盖。#13 容量门槛与 P6 本地实验条件继续单列。[W4 主线签收](W4_主线签收.md)。

- W5 候选 `455cf92` 的原生 JUnit 保留本机无 Docker sidecar 的停机用例跳过。相同通知断电场景在真实原生 Redis 停启、两次新 worker 和 PostgreSQL 中通过，正式托管 Docker 原用例也通过。历史资格观测只覆盖管理者停调，本轮验证了 Workshop 数据库实际拒绝连接期间的 NF 完成与原键恢复。[候选实测与原件](evidence/W5/candidate-455cf92/independent/README.md)。

- W5 合并点 `b30e76978830` 已通过候选两项 CI。原生本地 Docker 跳过与托管 PG 的 17 项命名空间跳过均在候选有逐项同源码补验，新的 main 重新验证这些范围。[合并双亲、时间与主线工作流](evidence/W5/main-b30e769/independent/merge-checkpoint.json)。

- W5 主线 `b30e76978830bec4c596c04194d749fd01f67402` 的本地 Docker 项另经原生 Redis 进程停启复验通过。托管 PG 的 17 项宿主限制已同 SHA 逐节点对应。既有 AT-30 性能事实与 Issue #13 继续保留。[W5 主线签收](W5_主线签收.md)。

W6 已复现主管调任后的两项不相容岗位扩编，修复及红绿原件可追溯。[红绿 JUnit、复演脚本与 633cf1a 补丁](evidence/W6/preset-supervisor/W6-preset-supervisor-633cf1a.zip)。八条旧库升级路径还发现两套同名 w009_rehearsal 中早期部署缺少岗位列和唯一索引，按真实旧库修复复验。AT-30 保持 Issue #13 原始口径。

首轮候选 23d195d 暴露两个验收入口故障。容量脚本从 Workshop 目录导入共享物理恢复模块时缺仓库根模块路径，长篇场景在持久化的下次可查询时间之前取回 IDLE。同条件修正后原始结算断言通过，产品的退避、授权和容量阈值均保持原值。 [当时的正式工作流与两项失败](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43116965757)。
