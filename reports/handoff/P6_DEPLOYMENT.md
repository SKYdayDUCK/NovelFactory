# 运行边界与发布核对

## 部署入口

先按 `workshop/docs/operations.md` 配置不同的 NF 与 Workshop 服务账户、PostgreSQL 17 数据库和只可经 stdio 使用的 broker。NF 的迁移及 supervisor 在 NF 虚拟环境执行，Workshop 使用自己的 wheel、迁移和环境。部署 W6 时核对 `aw-migrate current` 返回 `w008`。作品登记和继续运行的命令见 `P6_RUNTIME.md` 与 `docs/P6_本地真实模型创作交接.md`。实验封套与场景登记文件仅放在作者私有目录。

| 操作主体 | 允许操作 | 权限或账本证据 |
|---|---|---|
| 作者和 workshop_manager | 指定目标、查询已授权任务与结果、要求恢复 | 绑定安装、员工任期、幂等恢复记录和只读历史游标 |
| 普通员工和演员 | 在当前路由上收发指定类型的消息；申请映射到受权角色的任务 | 活任期、消息方向/类型/TTL、有效授权 epoch；角色世界知识由 NF 按角色切面生成 |
| 审核主管与独立审核员 | 对冻结版本请求或执行范围内审核，记录退场后的转交与超期 | 生产者及实质代理回避，冻结来源、资料 SHA、证据引用、期限与费用 |
| Workshop 委派 worker | 使用安装绑定的 launcher 派发一次受控任务，按原 operation 查询 | 工作槽、作品/分支/代际、单事务 NF task 与 receipt，未知保留原 hold |
| NF supervisor | 按冻结策略调度角色，提交 NF 领域的裁决和正式接受 | NF 原任务、角色授权代际、私有沙箱、同事务 S9 守卫和计费 attempt |
| 运维恢复身份 | 在隔离副本核对两侧并显式恢复派工 | PG 备份 manifest、WAL LSN、冻结 epoch 中的逐 operation 和 attempt 账本 |

## 上线与异常处理

- 启动前记下两个 Git SHA、数据库迁移头及对象快照标识。运行部署手册中的 stdio 冒烟和双向数据库拒绝测试，再按 `P6_RUNTIME.md` 校验实验材料、HTTP 模型绑定和金额/Token 授权。
- 用同一作者请求键继续。场景和任务进度可凭原 operation 查询。服务停止与 systemd timer 的命令见部署手册。启动时禁止未经对账重发 UNKNOWN 模型请求。
- 一个数据库恢复到旧时间点时，先关闭两端派工，在新实例校验备份。两套账本按原 task、generation、attempt、金额与 Token 校对。相同 receipt 但结算不同也会冻结。恢复证据在 `evidence/workshop/recovery/paired.json`。保留原数据库与对象历史直至新实例验收完毕。
- 原 NF 200/500/1000-agent 范围与工坊 10/50/200 人范围有不同负载、计时和验收口径。原失败与后续变动均以各 GitHub run 与 issue #13 为据。不得用工坊消息吞吐推断正式创作使用的 Token 或费用。
- 这次工程场景保留世界事件、角色可见性、定向修复、独立任务与账本证据。真实小说正文、供应商实际费用和两份独立文学审读仍待作者授权的作品现场产生。
