| 需求/验收 | 本次证据 | 状态 |
|---|---|---|
| FR-ENTRY, FR-COG / AT-01,08 | evidence/W0/nf-stdio.xml，项目与角色绑定，撤权、不可跨任务 | PASS（四项专项） |
| FR-AW/W0 | evidence/W0/workshop-stdio.xml，协商、未知版本、额外字段、在线撤权 | PASS（两项专项） |
| FR-SEM / AT-31–39 | 见 PR #6，三项容量工作流失败 | PARTIAL |
| FR-XSYS / AT-46,47 | evidence/workshop/recovery/paired.json，两个命名 WAL 目标、三种错位、终态账本校验及服务侧派工冻结 | PASS（本地物理演练） |
| FR-EXP / AT-50 | 缺项目消费限额、端点、双人审读 | BLOCKED_ENVIRONMENT |
| FR-AW-01–04 / AT-40 | evidence/workshop/w1/integrated.xml；四预设、CAS 竞争、失效任期、读者范围、真实 stdio | PASS（专项） |
| FR-AW-05–09 / AT-41–43 | evidence/workshop/w2/junit.xml；越级/任期/临时授权/回执/Redis 故障 | PASS（专项） |
| FR-XSYS-01–07 / AT-46,47 | evidence/workshop/w3/{dual,saga}.xml；独立数据库角色、双 MCP、原键回收、Hold 与 NF 取消结算；双库 PITR 见恢复专项 | PASS（专项） |
| FR-AW-10–13,17 / AT-44,45 | evidence/workshop/w4/staged.xml；五类分享、冻结哈希、七类审核、实质代理回避、审核专属额度守卫 | PASS（本地专项） |
| FR-AW-14–16,18 / AT-48,49 | evidence/workshop/w5；持久 wake request，重启后恢复到真实 NF，空闲调用计数、隔离游标与固定切面 | PASS（专项） |
| FR-XSYS, FR-AW / AT-40–49 | evidence/workshop/w6；Minimal 和 Standard 双向场景、三演员、S9 和独审回执。单独的 10/50/200 消息、逻辑委托与预算试验 | PASS（本地，远端最终 SHA 另记） |
| FR-AW / AT-44,45 | evidence/workshop/w6/independent-review.md、独审回归；源材料风险、审核人退场、截止期限、可信审批 | PASS（工程身份） |
| FR-ENTRY, FR-XSYS / AT-46–49 | evidence/workshop/w6/w008；相同作者键以安装分别幂等、隔离历史和进度、两套 stdio、审读冻结材料与取件 | PASS（PG17.11） |
| FR-EXP / AT-50 | reports/handoff/P6_ROUTE_PROBE.json、P6_LOCAL_PREFLIGHT.json；金额、模型、演员、作品 revision 和 Skill 固定检查 | 离线前检已实跑；商业运行与独立审读 BLOCKED_ENVIRONMENT |
| FR-SEM / AT-31–39 | evidence/p55/s9-local/remote-55d897；冻结的三样本四臂实际判决中 A、B、C、D 的 FP/FN 分别为 1/0、0/1、0/1、0/0 | 仅限已标注样本；实际小说总体误漏和供应商用量未测 |
| FR-SEM / AT-31–39 | evidence/p55/business/receipts；实际 writer、合成 provider、冻结金标和每臂有效吞吐/返工 Token | PASS（受控实验，零商业调用） |
| NF 历史 500 人读 / #13 | evidence/p55/http-validation 及原始运行 43040949167、43041082878；相同负载下完整 HTTP 记录 | #21 两次通过；组合源码仍按自己的 CI 验收 |
