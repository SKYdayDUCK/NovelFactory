# P5 最终集成与 PR 交接

交付位于 [Draft PR #5](https://github.com/SKYdayDUCK/NovelFactory/pull/5) 的 `dev/p5-agent-runtime`。接管点为 `73cdc997759c39e3e5dffe0ef0c4e0a42970f93f`。所有增量通过 GitHub Git Data API 提交并非强制推进原分支。接管时的分支、五次基线运行及本地历史清点见[恢复记录](P5_新会话接管与工作恢复.md)。正式基线与 Scion 实验分别保留在 [main](https://github.com/SKYdayDUCK/NovelFactory/tree/main) 与 [PR #4](https://github.com/SKYdayDUCK/NovelFactory/pull/4)。最终提交的逐项 Actions 状态和原始 artifact 可从 [PR 检查页](https://github.com/SKYdayDUCK/NovelFactory/pull/5/checks) 打开。PR 正文固定本次交付 SHA 与对应运行。

## 作者怎样恢复作品

管理员首次按[部署与诊断](../adapters/codex/P5-stdio.md)在服务端运行迁移、绑定作品和分支、签发限定范围和有效期的 resume_operator 授权，并启用独立 Supervisor。作者账户使用专用 SSH 别名。在自己的项目根目录执行一次 `.venv/bin/python scripts/install_p5_project.py --project . --ssh-alias nf-resume-service` 后，用 VS Code 打开并信任该项目，在官方 Codex 侧栏输入 **继续未完成的任务**。已有相同配置时安装器返回 ALREADY_CONFIGURED；配置冲突会保留原文件并说明原因。

扩展调用 `nf_resume_project({})`。返回值含项目态、READY/WAITING/RUNNING、唤醒与恢复数、阻断摘要、待审数、作者审批数及恢复授权到期时间。等待消息时可以关闭编辑器。数据库和独立服务仍运行时，提交事件会唤醒符合条件的任务；主机关机后要等服务重启才能对账。在服务端可用 `scripts/probe_codex_mcp.py --binding /服务端私有路径/resume.json --status-only` 通过实际 app-server 查询进度。

## A–M 验收

| 场景 | 结论 | 实际观察及边界 |
|---|---|---|
| A 重复恢复 | DONE | 项目工具只接受服务端绑定。基线官方 Web 扩展重复中文指令的唤醒数为 1、0。本轮原生 app-server 另在无新事件的作品连续恢复，两次均为 READY 1 / WAITING 1 / RUNNING 0，新增模型轮次 0。见[原生回执](../evidence/p5/final/native-resume.json)。 |
| B 休眠、K 空闲 | DONE（45 秒专项） | B 在消息到达前、卷纲在世界接受前没有模型执行。60 秒对账周期内，连续观察 45 秒，连接池 SQL、新任务、模型调用和悬挂事务均为 0。强制断开 LISTEN 后 5.309 秒内完成第二项任务。见[原始观测](../evidence/p5/scheduler/idle.json)。 |
| C 拒绝原计划、E 分层审核 | DONE（合成剧情） | 角色可以拒绝赴宴。导演拿到正式事件，上级在有据且获准的 K 场窗口内审批，支持回归、分化或暂不能判断。重试不能改写不可变参考线或已接受历史。见[连续推进与偏离证据](P5_自动剧情连续推进验收.md)。 |
| D 同物品争夺 | DONE（授权赠与） | 两名角色分别申领，一个接收者和一次 Kernel 转移。双赢家、名单外接收者和错误持有人均无法提交世界效果。用例见[联合裁决](../tests/integration/test_p5_joint_handoff.py)。 |
| F 两场连续运行 | DONE（预授权窗口） | 服务在实际过程、故事时间、计划节点和预算均有效时只展开下一场。错误触发器记录停止原因；重启和重复登记不重演上一场。 |
| G 并发 | DONE（小规模实际 Host） | 两个实际 Codex 角色进程同时工作。数据库限制全局、作品、岗位并发，多个 Supervisor 不能重复领取同任务。锁住队头后仍可领取其他合格任务。单场 8 个独立 Thread/Turn、一次正式接受。见[调度证据](P5_调度与事件唤醒收尾.md)。 |
| H 认知隔离、M 执行隔离 | DONE（Linux 等效容器） | 实际受限 app-server 只读本角色、本任务、本时点的 BELIEF。真实与虚构的未授权对象返回相同拒绝；撤权后读取停止。Linux 命名空间内的测试角色无法读取服务端文件、环境、Shell 或连接测试数据库。双系统账户 SSH 在用户设备上的部署尚未实测。见[角色验收](P5_MCP与Codex角色隔离验收.md)。 |
| I 分阶段故障 | DONE（隔离数据） | 覆盖领取后中断、发送结果未知、结果已结算未交付、候选持久化和 Kernel 接受前后。UNKNOWN 保留预留，旧代停止派发；同一有效租约可以取回已保存结果。详见[故障矩阵](P5_成本性能与故障恢复.md)。 |
| J 原生 IDE | DONE（普通 Linux 测试账户、合成模型） | UID 1001 在实际 VS Code 桌面 1.145.3 / 官方 Codex 扩展 26.51106.51428 的侧栏两次输入中文指令。工作台显示两轮；MCP 发现两个工具，两次空参数恢复分别唤醒 1、0 项，最终 READY 1、ACCEPTED 1。PG 17.11 仅确认一个事件，作品侧模型账本新增 0。独立 SSH 生产环境和付费模型尚未部署验收。[成功运行与原始 artifact](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42982135338) |
| L 跨卷 | DONE（有界链路） | 在第一卷开场前授权下一卷。末场重大偏离和编辑审批后生成结算，三段夹具正文走原生版本发布；不同测试身份阅读、审核后建立连续性快照。下卷只收到允许的前卷材料，生成一个 DRAFT 窗口。六次合成请求中下卷一次，空闲重放零次。见[两卷回执](../evidence/p5/story/r4_two_volumes.json)。 |

角色执行有三种显式预算选择。原 `codex-text` 每个任务一次模型调用；`codex-role-mcp` 在一次模型发送前读取获准上下文；`codex-role-tools` 使用最多两次各自记账的模型调用，先决定是否执行一次 BELIEF 查询，再生成行动。第一步已发送而结果未知、第二步撤权、超出任务或作品额度时均停止。过期代际中已计费但未完整交付的选择仍需对账。虚构文本的文学质量由实际作品审读者判断。正式供应商无已授权端点与可核对账单，本轮真实商业推理、计费和长篇文学验收为 BLOCKED_ENVIRONMENT。[模型环境实测](../evidence/p5/final/model-preflight.json)

## 完整验证及未达项

在已发布的功能提交 `50994d9` 上从干净工作树执行 `pytest tests -q`，本地 **721 passed、0 failed、0 skipped**，503.08 秒。使用实际 PG 17.11、Python 3.12.14、Codex CLI 0.167.0-alpha.4，以及隔离的回环供应商。一条既有 Starlette 测试客户端弃用提示。该套件包含前述专项，计数不相加。[逐项 JUnit](../evidence/p5/final/postgres-50994d9.xml) · [计数与文件校验和](../evidence/p5/final/postgres-50994d9.json)

独立检出 `50994d9e7559a7c151ed75bd5a285478bbf9343a` 进行真实物理恢复。PG 17.11 对 45 张表和 19 个对象逐项比对，4 个 WAL 归档成功，目标前提交丢失 0 个，刻意排除目标后提交 1 个。保留 1 个 UNKNOWN 及 0.5 预算预留，旧 worker 被拒。恢复至可验证服务 1.179 秒，仅代表本次隔离样本。独立契约验证覆盖 15 份 Schema、209 个定义、84 个工具绑定；独立机制测试 35/35，其中含原有 18 项。[带源码校验的恢复记录](../evidence/p5/final/verified-50994d9.json)

三个既有容量工作流使用原阈值。在同一完成检查点 `a4c7f83`，P2 的 200 overload 两次首次请求发起速率为 40.90 与 40.05 次/秒，门槛 950 次/秒；P3/P4 的 200 overload 为 43.45；P3 的 1000 开放到达实际发出 42.55 次/秒。500 持续读在此检查点通过，在另一次运行出现 1 次 HTTP 429。P2 正确性与有界恢复通过，容量仍标记 NOT_MET。[P2 运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42978787637) · [P3/P4 运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42978787643) · [P3 规模运行](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42978787652) · [500 持续读波动](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42979118359)。 项目调度的小规模并发测量不能外推到几百名 Agent。两场、两卷测试也不代表可以无授权生成任意长度小说。生产部署仍需要服务环境与作品预算；超过授权的改动、文学质量签核和最终合并由相应授权人决定。

## 提交与复核

| 检查点 | 变更和验证入口 |
|---|---|
| R0、R1 | `2b4f282` 恢复基线，`6b90bbb` 补充屏障关闭和恢复诊断。[逐项差距](P5_功能完成度与差距审计.md) |
| R2 | `124dab2` 事件通知和有界调度，`95ab7bc` 解除队首锁阻塞，`7833858` 持久化超时和下卷终止。[调度报告](P5_调度与事件唤醒收尾.md) |
| R3 | `c7c23a4` 隔离 Host，`ed589ab` 普通派发读取，`532314c` 模型选择读取，`16e787d` 单步额度与授权竞态。[角色报告](P5_MCP与Codex角色隔离验收.md) |
| R4 | `8bebec7` 后继任务，`88eb506` 卷终结算，`4a3b7ea` 自动发布精确的已审快照，`50994d9` 修复世界继续推进后找回原审核回执。[剧情报告](P5_自动剧情连续推进验收.md) |
| R5 | `8954692` 同物品竞争和只读原生诊断，`a4c7f83` 结构化偏离审核，`ae1d246` 提示成本及 PITR。八轮输出契约字节减少 10.1%，真实 Token 降幅未测。[成本报告](P5_成本性能与故障恢复.md) |
| R6 | `3e6c5d4` 项目配置安装与运行说明，`23540ae` 原生桌面验收入口，`ca17427` 核验回环设备，`730e652` 安装桌面沙箱助手，`3119df2` 配齐 CI 角色隔离依赖，`1823668` 完成两轮原生桌面 UI/MCP 取证。 |

中间提交 `ed589ab` 曾提前引用后一检查点的输出契约，单独检出时 MCP 导入失败。`a4c7f83` 补齐契约；独立工作树重验了导入、MCP、角色读取与正式交付。判断可运行版本应使用完成集成的提交。七份交付报告、源文件和主要测试证据均保存在本 PR。
