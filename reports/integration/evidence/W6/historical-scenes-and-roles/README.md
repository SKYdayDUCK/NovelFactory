# W6 四种组织预设、角色认知和跨服务故障的历史与签收前预演

| 来源 | 核实结果 |
|---|---|
| 已有 W6 工程门禁 | `34e4e7658b8a6dba7055a6cab1c1a55971176bba` 的三个作业均成功，保留身份验证、原始和 matched 两组场景/容量工件。后续 `73050eab6c5b64b79e58d6d1db3931148cf61e83` 增加原生 Codex 双 SSH，历史官方回执留有真实线程、两个受限 OS 用户、两项实际 MCP 读取、私有绑定读取拒绝及零模型回合。另归档 43071101577 的 AT43/49 和 43075242671 原生双 SSH 原件。六个 ZIP 均以原 GitHub Actions SHA-256 校验。[作业及 SHA-256](hosted-artifacts.json) · [历史源码与运行](github-historical-runs.json) |
| 旧试次复跑 | 同一个 `73050ea` Git 检出提供原 Workshop 与 NF。PostgreSQL 17.11、Codex 0.167.0-alpha.4 和回环供应商。AT43/49 2/2、四预设 4/4、零跳过。这个旧数据库夹具在单个 PG 集群内创建两个具有独立 owner 的数据库。[历史复跑实测](independent-historical-73050ea.zip) |
| 从签收 W5 预演 | 以 W5 主线 `b30e76978830bec4c596c04194d749fd01f67402` 接入 W6、真实原生角色入口和拟议的迁移汇合点，构成独立本地测试提交 `207bc3e16b098e66c59ed007da077b0ef95a4fab`。两套独立 PG 集群、两套非超级用户服务身份，AT43/49 2/2、四预设 4/4、零跳过。wheel 在 checkout 外安装，以 Python -I 加载包内 MCP 目录和迁移并将空库升到 w010。该次资格早于正式发布候选。[全部原始 JUnit 与业务数据](w5-based-rehearsal.zip) |
| 四预设 | Minimal、Standard、Long-form、Research-Audit 各跑完整作者/经理请求及实收回执、委托、NF 任务、正式产物、S9 冲突与定向修复、独立审核和原键结算。每组 NF 最终 11 项任务接受且就绪/等待/运行均为零。原文本 SHA 与审核冻结 SHA 匹配，程序化独立审核签署后作者读取回执。18 个 token、0.020000 USD 为测试账单，每组供应商商业调用均为 0。没有作者或独立读者的文学盲评。 |
| AT43 与 AT49 | AT43 对照另一角色能读的 BELIEF，检查本角色实际 HTTP 模型输入和 MCP 读取中均无该私有事实，组织路由只出现在 assignment。AT49 在 NF RUNNING 且唯一 Attempt 已 SENT 时停止 Workshop 并阻断该库连接，NF 在这段时间接受任务；新 Workshop 进程按原操作键找回原回执，一次请求原样对账，保留 UNKNOWN 与最终结算两条不可变成本观察。 |
| 原生角色资格 | 两个新服务端数据库管理员仅用于准备；通过当前服务数据库名为两方独立验证 owner 与 PG 系统标识，并在创建 broker 子进程前剔除两份管理员环境。本地预演执行到 broker 子进程边界。此宿主无 sudo，三独立 OS 用户与真实 SSH/Codex 的历史运行及重新发布的正式 hosted 运行分别留证。[数据库实测](dual-admin-setup.json) · [原生 SSH 的原始来源](legacy-ssh-source.json) |

本地预演可从已签 W5 在新的 Git 索引中还原。将压缩补丁解压，应用后 Git 树应为 `af6f4ce2b7765e89e32bd4ed5a162b5893bf4c1f`，该树已独立重建核验。[完整差异](rehearsal-from-signed-w5.patch.gz)。设置 `AW_NF_SOURCE` 为检出根，`AW_DUAL_TEST_ADMIN_URL`、`AW_DUAL_TEST_NF_ADMIN_URL` 指向两套一次性集群，`AW_ACCEPTANCE_OUTPUT` 为私有输出目录。使用锁定的 NF 环境运行 Workshop 下 `benchmarks/actor_directory_isolation.py`、`benchmarks/running_workshop_outage.py` 和 `benchmarks/cross_service_scene.py`。各文件的原件校验和与独立数据在 ZIP 内的 manifest.json。
