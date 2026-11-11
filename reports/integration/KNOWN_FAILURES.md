# 已知失败

- AT-30，issue #13 开放。旧 50/200 热点和 1000 开环读存在原门槛未达到的问题；[指标、基线和历次失败样本](02_容量与实验原始证据.md)。
- P5.5 500 Agent、600 秒、20 RPS 的旧候选曾出现 492 与 46 次 HTTP 429；最终候选云端原条件 12,000/12,000，另一共享主机的同业务源码候选与 P5 对照均未达到原线。保留尾延迟、限流和 7 次传输异常的原始数据。[候选及合并后实测](p55-main/README.md)
- 冻结阶段的在途撤权、高风险审核及双库错位修复分散于后续提交，见 [Workshop 证据](01_Workshop及桥接证据.md)。在相关阶段合并前验证各项。
- 付费创作与人工文学验收尚无本轮授权和实际结果。合成路径按 fixture 登记。

- W1 旧候选在带延迟外键事件的历史事务中迁移失败（SQLSTATE 55006），`ac5962e0` 修复；最终 `48c20a10` 的同一失败用例及旧发行服务升级均在 PostgreSQL 17.11 上通过。[原故障和修复回执](evidence/W1/candidate-48c20a1/workshop-ci.json)

- W2 早期候选 `772ee36f` 的 PEL 测试错误地假定全库随机顺序前两项均可采纳；终态使用两笔真实授权事件，仍全量重放并覆盖拒绝和去重。[原失败](evidence/W2/candidate-162af26/previous-pel-failure.log) · [修复及主线验收](W2_主线签收.md)

- NF Bridge 原实现允许已过期安装触及新预留或发送。相同数据库和发送前拦截协议下，旧实现失败两项，修复后通过两项。此前 UNKNOWN 的原始费用仍可对账。[对照证据](https://github.com/SKYdayDUCK/NovelFactory/blob/e8529cd74cef2b8cf556396da8006409eeda66b0/reports/integration/evidence/NF-Bridge/candidate-26bf033/expiry-control.zip)

- 桥接初版 `26bf0337` 的托管 PG 套件有 1 项失败、805 项通过、17 项跳过。历史 S9 用例的目标迁移写成 head，导致以 0016 断言查验 0017。新版本将历史边界固定在 0016，另验从发布库升级到 0017、非空降级拒绝和原 67 张表内容。[失败与修复原件](https://github.com/SKYdayDUCK/NovelFactory/blob/e8529cd74cef2b8cf556396da8006409eeda66b0/reports/integration/evidence/NF-Bridge/review-26bf033/upgrade-controls.zip)

- NF Bridge 已修复出站权限变化、跨安装操作、旧升级端点及 S9 等待进度。已用独立本地实测覆盖托管跳过的 17 项 Linux 角色约束，四项 AT-30 原门槛仍保留。[主线签收](NF_Bridge_主线签收.md)

- W3 的预候选可在相同操作回执更换 WorldRef、终态额外出现 Attempt 或既有组织结算差额的情况下解除围栏。真实 PostgreSQL 对照中，原实现允许 NF 的 0.40 与 Workshop 的 0.20 同时保留。重新核对时须保持冻结，保护原始结算。旧版空 WorldRef 首次补记和一致回执有正向覆盖。
- W3 的正常导演在完成授权派工后曾无法交接给同域另一名在职演员。新用例通过真实 MCP 枚举并调用派工、交接和重复请求，另查当前路由、员工任期与操作所属安装。
