# W5 既有证据与离线资格预演

原 #14 的 `609e92278476c60a893658ef53b6ff15f659b153` 同时留下本地 33 项 Workshop 与 2 项双服务 JUnit，以及两条通过的托管工作流。安装和作者任期修复来自 `d308e1ce1986a33e4781be97fba8a862b89dd76f`。它与旧 W4 以双亲归并到 `bfb74f681435b12a385eb54e596c658e64629276`。[原 33 项](original-all.xml) · [原双服务](original-dual.xml) · [两条正式 run 与四份原始 ZIP 的 SHA-256](ci.json)

旧资格工作流 `43072737280` 从 `bc6fbbe71078b825872d313d5ef4f2d95aa2ddcb` 构造候选 `85e443d68cd242595e124dcaca27844d34ddba39`，Git 树 `c9f09eb2295c098710c6109efff7d03ebe55b210`。原观测器在两次 NF 扫描之间仍可连接 Workshop 数据库。它证明管理者停调时 NF 自主推进。本文分别保留该工作流的原回执与强化后的实际拒绝连接试次。[旧 CI 原件](43072737280-workshop-results.zip)

独立预演在 PostgreSQL 17.11、两套真实 stdio 和原生 manager CLI 上得到 2/2，零跳过。物理恢复使用一个独立测试集群内的两数据库，在该集群临时关闭 Workshop 库的新连接，确认零活动 Workshop 客户端和真实作者 MCP 拒绝。NF 完成 READY → RESULT_STORED → ACCEPTED，依原键取得唯一 Attempt；Workshop 恢复后作者待办 ID、任务 ID、回执不变，先恢复 HELD/UNKNOWN，再按原发票结算。两个 WAL 目标精确回放。独立双集群测试另行验证作者通过安装的 CLI 触达 NF、空闲进程不启动对端。商业供应商调用为零。这次预演早于公开候选。[精确代码与测试证据](rehearsal.json) · [JUnit](w5.xml) · [完整 WAL](paired.json)

原始两条候选 CI [Workshop](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43033428508) · [NF](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43033428612) · [历史资格作业](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43072737280)
