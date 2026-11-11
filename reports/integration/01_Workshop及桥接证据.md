# Workshop、NF 委托桥接与冻结的 P6 证据

这是 P5.5 接管时对存量分支的只读清单。下列 PR 源码、工作流和既有回执按 SHA 定位。各阶段的合并后 `main` 验收均待执行。阶段只能在前一阶段正式合并、验证、登记后激活。

## 规格与准入依据

v2.3 增量修订包 SHA-256 为 `00ed0fa956dfd1298f9f26b2e779aedab404a42e6b601103320427d8f9f2c63e`。已核对 `01` 需求分析、`02` 系统设计 §15.9、`03` 工坊接口规格 §8、`04` S9 验收基线 §6–7 和 `05` 追踪矩阵。原接管包 SHA-256 为 `336a2a061f470f953183b9241e7fb34b317157c454a0578eae719a8741076774`。v2.3 的 AW_TARGET、旧 PR 状态属于设计冻结点。下表使用当前分支实物更新实现证据。

FR-EXP-07 / AT-30 保留历史门槛与 NOT_MET。S9 §7 要求新路径无未解释回归，旧标准失败单列。FR-SEM-01～16 / AT-31～39 另有功能、权限、历史、恢复、同版本测试和独立审查要求。原接管提示词 §六及原 S9 任务 §12.1 要求保留强制保护；组织审批与分支保护仍按实际仓库执行。[仓库中的工坊退出标准](https://github.com/SKYdayDUCK/NovelFactory/blob/97eeceac266016b0d5da62786b019b704bfcdb70/docs/P5.6_开发阶段与验收计划.md)

## 分阶段实现与证据

表中路径以各链接指定的 Git 树为根。AW 源码目录是 `workshop/src/agent_workshop/`，AW 迁移目录是 `workshop/migrations/versions/`。JUnit 数字来自回执的 tests/failures/errors/skipped 字段；表中列出的本地套件均为零失败、零错误、零跳过。本地回执未嵌入执行 SHA 时，仅指出其归档版本。Actions 列的工作流另行按 PR head 查询，区分本地证据、PR CI 与合并后验收。

| 阶段 / 原始需求 | 候选 Git 树 | 入口、迁移与行为 | 已归档原始数据及对应 CI |
|---|---|---|---|
| W0 · 契约及 FR-ENTRY/COG · #7 | [`c18e53959807a35816eaa1258a3b7c64b80b4637`](https://github.com/SKYdayDUCK/NovelFactory/tree/c18e53959807a35816eaa1258a3b7c64b80b4637) | server.py / auth.py、`w000_installations`。实际 stdio 协商、运行时绑定及撤权，角色与项目发现分离。 | PG 17.11、Python 3.12.14；NF 绑定到 `b3b0e9b08a4ef2160a70fd858ca64de1fd58b6a5`，NF 4 项、AW 2 项，模型调用 0。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/c18e53959807a35816eaa1258a3b7c64b80b4637/evidence/W0/run.json) · [Actions 43030289759](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43030289759) |
| W1 · FR-AW-01～04 / AT-40 · #8 | [`f59ac5e442bd5a434ded2e65526d5750c5488e9b`](https://github.com/SKYdayDUCK/NovelFactory/tree/f59ac5e442bd5a434ded2e65526d5750c5488e9b) | organization.py、`w001_organization`。四预设、员工任期、组织 CAS、竞争雇佣和名册可见域。 | PG 17.11，从 w000 升级；集成 JUnit 9 项。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/f59ac5e442bd5a434ded2e65526d5750c5488e9b/evidence/workshop/w1/integrated.xml) · [Actions 43030513699](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43030513699) |
| W2 · FR-AW-05～09 / AT-41～43 · #9 | [`0da5b7c8fab5321fa50d2a04a516f3fd95432539`](https://github.com/SKYdayDUCK/NovelFactory/tree/0da5b7c8fab5321fa50d2a04a516f3fd95432539) | messaging.py / notifications.py、`w002_messaging`。逐边路由、有限次授权、ACK、PEL 和 SQL 重建。 | PG 17.11、Redis 8.0.2；13 项含 W1 的 7 项。首次 Redis 启动缺动态库记录在同目录。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/0da5b7c8fab5321fa50d2a04a516f3fd95432539/evidence/workshop/w2/junit.xml) · [Actions 43031076636](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43031076636) |
| NF Bridge · FR-XSYS-01～07 / AT-46、47 · #11 | [`90cac947c6f1b176f20df6a579027f24e96d7b4c`](https://github.com/SKYdayDUCK/NovelFactory/tree/90cac947c6f1b176f20df6a579027f24e96d7b4c) | NF `application/delegation.py`、`interfaces/delegation_stdio.py`、`0017_nf_workshop_delegation`。同事务创建任务、Supervisor、回执与 Outbox；受限派发/查询及账本核验。 | 45 项为早期 `795b17f2` 归档证据。当前 head 已含 P5.5 `97eeceac`；此头的完整性、PITR、容量分别复核。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/90cac947c6f1b176f20df6a579027f24e96d7b4c/evidence/workshop-delegation/pg17.xml) · [Actions 43047258552](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43047258552) |
| W3 · FR-XSYS-01～07 / AT-46、47 · #10 | [`15d1cf51c357627866d06bd49fd326798dda56a3`](https://github.com/SKYdayDUCK/NovelFactory/tree/15d1cf51c357627866d06bd49fd326798dda56a3) | delegation.py / reconcile.py、`w003_delegations`。跨服务意图、原键对账、预算 hold、交接。 | PG 17.11，两套非超级用户和数据库，双向禁止跨库 CONNECT；双 stdio 丢回包/找回原任务 1 项，本地 saga 4 项。早期 CI 未包含该双服务步骤。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/15d1cf51c357627866d06bd49fd326798dda56a3/evidence/workshop/w3/dual.xml) · [Actions 43032253677](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43032253677) |
| W4 · FR-AW-10～13、17 / AT-44、45 · #12 | [`71b471416c9407e348d4bdce817588def5f11d5b`](https://github.com/SKYdayDUCK/NovelFactory/tree/71b471416c9407e348d4bdce817588def5f11d5b) | reviews_skills.py / review_admin.py、`w004_reviews_skills`。五级 Skill 策略、七审核模式、身份组回避与专项预算。 | PG 17.11、Redis；32 项。该 CI 附带实际双 stdio，NF 对端固定早期 `795b17f2`。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/71b471416c9407e348d4bdce817588def5f11d5b/evidence/workshop/w4/staged.xml) · [Actions 43032647213](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43032647213) |
| W5 · FR-AW-14～16、18 / AT-48、49 · #14 | [`609e92278476c60a893658ef53b6ff15f659b153`](https://github.com/SKYdayDUCK/NovelFactory/tree/609e92278476c60a893658ef53b6ff15f659b153) | manager.py / manager_worker.py、`w005_manager`。单次处理持久唤醒，范围绑定历史游标；作者重连后原键续跑。 | PG 17.11、Redis 8；33 项及同目录 dual.xml 2 项。空闲第二轮零 NF 调用。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/blob/609e92278476c60a893658ef53b6ff15f659b153/evidence/workshop/w5/all.xml) · [Actions 43033428508](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43033428508) |
| W6 · FR-AW、FR-XSYS / AT-40～49 · #15 | [`a0d8c7e0ac4e775fdb3190af6f254aecac0ca256`](https://github.com/SKYdayDUCK/NovelFactory/tree/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256) | presets.py / recovery.py / reconcile.py、w006～w008。四预设变更围栏、双库物理恢复、安装隔离及两套剧情闭环。 | PG 17.11、Redis 8；44 项、双服务/剧情 4 项，物理恢复单独执行。现 head 的 Workshop 作业成功，含 wheel 迁移、工作组、双库、两个场景与三档容量。 [原始回执](https://github.com/SKYdayDUCK/NovelFactory/tree/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/evidence/workshop/w6/w008) · [Actions 43038309410](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43038309410) |

W0 的本地 `run.json` 只列出两个项目工具和四个角色工具，双服务 Saga 明记 NOT_EXECUTED。轮到 W0 后，按实际授予范围补齐规格中九个既有工具的发现、成功调用和拒绝矩阵。旧 W3 的本地 UNKNOWN 有明确的注入响应，NF Bridge 的费用依据以权威 attempt 账本核对。W5 的冻结游标/空闲恢复专项只能覆盖其测试场景，AT-49 还需在合并候选核对 NF 在 AW 停机期间独立推进后的对账。

## 分支间的正确性依赖

- W2 之后的 W6 代码补足了发件来源撤权时对在途任务、取件和 ACK 的围栏。轮到 W2 时核对原发件人撤权反例，纳入与 w002 历史邮件兼容的最小修复。
- W3 的 AT-47 包含双方 PITR 落点不同以及恢复后的派工冻结。首个 W3 双 stdio 测试仅覆盖失回应。物理演练和终态账本一致性保护出现在 W6：同一 task/receipt 仍存在但 attempt 已丢失时必须保持冻结。应在 W3 对应阶段获得这一退出条件的实证，或将所需的既存修复按依赖提取。
- W4 首版的七种审核模式与身份组检查均有实测。W6 独立审查另记录了高影响来源被降级、审核材料缺读取途径和审核员退出后任务滞留的修复。W4 启用前对照既有 `test_caller_cannot_downgrade_sensitive_source_to_routine_rule_pass`、`test_review_get_reassignment_and_offline_expiry` 及源材料读取测试，补齐本阶段范围。
- W6 CI 会优先使用合并树内的 NF 委托入口；独立 AW 分支使用固定 NF `74350760dfd063f3596e5b6838a56a40d3006ad6`。合并后记录**实际选择**的 NF 与 AW SHA。归档 `w008/scene-standard.json` 的 NF 为 `becc8e7a400756c67832e0aca16cac78000df661`，AW 为 `ed6ace9ec14c204aa9fda88ccaa9dee7d3bf4594` 加未提交改动，文件记录实际源码 SHA-256。它只作为旧场景回执。

[W6 既有独立审查与修复](https://github.com/SKYdayDUCK/NovelFactory/blob/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/evidence/workshop/w6/independent-review.md) · [双服务工作流与对端选择](https://github.com/SKYdayDUCK/NovelFactory/blob/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/.github/workflows/agent-workshop.yml) · [物理回执](https://github.com/SKYdayDUCK/NovelFactory/blob/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/evidence/workshop/recovery/paired.json)

## W6 容量、恢复及真实模型边界

消息负载采用 Minimal、8 worker、每员工 3 轮，发送及确认 P99 原门槛各为 2,000 ms，通知恢复不超过 60 秒，确认率 100%，重复效果为 0。`clean-w006-rerun` 记录执行头 `d5bcb7cb0a1e9030b122561e32c90c0b182088ec`、PG17.11、Redis8.0.2、Python3.12.14，计时包含鉴权和 SQL 分发，不含 stdio。逻辑委托按固定 1 个在途合成模型、每档 600 秒、100% 完成、无重复任务/未结预留、禁止额外授权，每项任务 1 次提供方请求。后者统计从委托至结算的排队时间。

| 员工数 | 消息完成/计划 | 唯一委托/已结 | 委托墙钟秒 | 合成模型峰值 |
|---:|---:|---:|---:|---:|
| 10 | 30/30 | 10/10 | 3.09 | 1 |
| 50 | 150/150 | 50/50 | 12.63 | 1 |
| 200 | 600/600 | 200/200 | 52.44 | 1 |

[消息原始记录](https://github.com/SKYdayDUCK/NovelFactory/tree/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/evidence/workshop/w6/clean-w006-rerun) · [200 委托原始记录](https://github.com/SKYdayDUCK/NovelFactory/blob/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/evidence/workshop/w6/delegations-200.json) · [负载和门槛原件](https://github.com/SKYdayDUCK/NovelFactory/tree/a0d8c7e0ac4e775fdb3190af6f254aecac0ca256/workshop/benchmarks)

物理备份 `pg_verifybackup` exit 0，实际回放到 `0/40293E0` 和 `0/4058A58`，覆盖三种错位。Standard 剧情包含三个密封行动、角色偏离和世界接受；S9 个案记录 `ESCALATE_REVIEW / REVIEW_REQUIRED`，因此该个案仅证明检测并升级。局部修复终态应在最终 main 上另以已应用的 PlanRevision 或修复收据证明。商业推理和真实双人文学审读继续 `BLOCKED_ENVIRONMENT`，所有这些合成统计仅用于工程验收。

## P6 存量

#17 `43dc369ab73e2faacb509b6ca16995848fc92c28` 含 NF 0018 共享预算迁移、限额和 UNKNOWN 保留。#16 `1d950705ccdebaca02171bd24293682f6efeab54` 含离线预检、部署与恢复工具，依赖已整合的 Workshop 与 NF 预算。两分支保留。P5.6 主分支完成最终验收后才启动 P6；实际付费验收须具备绑定作品、模型授权、支出上限及读者。
