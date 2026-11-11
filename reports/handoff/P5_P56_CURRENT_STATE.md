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
