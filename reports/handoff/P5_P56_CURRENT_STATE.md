# 接管实查

| 对象 | 起始提交 | GitHub / 本地证据 | 结论 |
|---|---|---|---|
| main | c8d35c74c342b58fd72fa379f18ac1b480eed468 | git ls-remote HEAD | P0–P4 |
| P5 | 9dc9c6b806b8ac43f56d0893194a5b402f3f16ca | PR #5，base main，6 个 workflow 各 3 成功与失败 | 待修复、复测 |
| P5.5 | b3b0e9b08a4ef2160a70fd858ca64de1fd58b6a5 | PR #6，base dev/p5-agent-runtime，工程、PITR、桌面成功，P2 容量、P3 规模、P3/P4 测量失败 | 容量 NOT_MET |
| 恢复 | b3b0e9b0 | 前任主工作树 39 staged + 3 untracked；独立工作树保全 | 证据不得归为本轮新测 |
| Agent Workshop | W0 分支自 main 分出 | 独立仓库查询 404，已安装的 GitHub 权限未提供创建仓库操作，CLI 无写入登录；使用已授权仓库子项目 | 独立数据、进程、迁移与发布边界 |
| Scion | 4f249d5 | PR #4 保持独立 | 未纳入本轮 |

冻结输入：v2.3 设计包 SHA256 `00ed0fa956dfd1298f9f26b2e779aedab404a42e6b601103320427d8f9f2c63e`。独立下载的 00 与上传执行指令逐字节相同。Workshop 对照 `workshop/contracts/nf-catalog.json`。各阶段以 STAGE_STATUS.json 与 evidence/ 为准，合并阻断按原门槛处理。P6 缺本作品的模型端点与消费限额及实际双人审读。

W0 提交 c18e53959807a35816eaa1258a3b7c64b80b4637，PR #7，独立 PostgreSQL 与 stdio 工作流 43030289759 成功。W1 在干净 PG17.11 依序升级，通过 9 项组织与 stdio 检查，原始报告位于 evidence/workshop/w1。

W1 PR #8 的 f59ac5e44 通过 Workshop CI 43030513699。W2 从 w001 迁移至 w002，PG17.11 与 Redis 8.0.2 上 13 项通过，包含有限次数路由竞争、重复投递、消费中断和删流后恢复。

W2 PR #9 (0da5b7c8), Workshop CI 43031076636 通过。W3 的双库独立身份、双侧 stdio、NF 成功后丢包、原操作号查回、NF 取消与组织结算专项已在本地通过，见 evidence/workshop/w3。P5 新提交修复了续租时并发槽提前释放，P5.5 分开归档了恢复数据与固定 WorldRef 查询优化。

W3 两端已建立 PR #10 和 #11。NF 接受侧 795b17f2 有 45 项本地专项通过。Workshop 独立工作流 43032253677 成功。W4 加入按 NF 原始 SHA 签出的双服务 CI。W4 实际迁移至 w004，在 PG17/Redis8 全部 32 项测试通过。

阶段 A 已合并：PR #5 的 f2e6d38 在 main 中的合并点 a8f37b72，安全/完整性、PITR 和桌面工作流通过，三项历史容量工作流仍红。W4 PR #12（71b47141）专用 CI 43032647213 通过，包含固定 NF 795b17f2 的跨数据库双 stdio 步骤。W5 的作者唤醒记录、隔离历史、同水位分页与 manager 服务已接入。

W5 PR #14 是 609e9227。W6 两种 Preset 贯通案例及 10/50/200 员工消息与端到端委托压测已在隔离环境实跑。三档分别完成 10/50/200 个唯一委托并结算，固定一个合成模型并发，200 档 52.44 秒。两个独立数据库、两套账号和真实 stdio 与生产模型凭据是不同的验收项。初次无价卡夹具正确保留 UNKNOWN，见失败记录。

W6 的 PR #15 起始提交 b05ea32a，独审修复 c8ae27b6。初次远端工作流 43034935188 的安装 wheel、迁移和 Workshop 单测绿色，runner 下载 bubblewrap 时无法解析 Azure Ubuntu 镜像；双服务步骤没有执行。本轮已更改 runner 镜像选择，并把完整的物理恢复与三档消息负载纳入该工作流。W6 后续本地在 PG17.11/Redis8 完成 44 个 Workshop 检查、4 个双服务/剧情检查和 1 个双目标 WAL PITR。w008 将同组织不同安装的委派、历史查询和恢复幂等键隔离。独立演练故意还原至“相同收据、尚无 attempt”的 NF 时点，确认连已结算的原操作也阻止解冻。工作流固定 NF becc8e7a，最终运行 ID 和合并以之后状态记录为准。

PR #6 已对 main，第二项投影完整性与热读修复 e2f0faba，独立 PG 检查 33/33。现行阈值与此前失败独立保留，不在这里把完整 600 秒门槛提前判为通过。

W6 头 657a4e1e（PR #15），对应独立工作流 43036228913。阶段 C 的两份私有输入模板、零请求前检与 5 组离线正负例均已建立；首轮 HTTP 路由回执和空模板回执都记录 BLOCKED_ENVIRONMENT。作品场景数 0，已分配独立文学审读人数 0，商业账单金额未测。NF 全卷共享 Token 封顶的后续代码另行记录。
