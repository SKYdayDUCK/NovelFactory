# W3 公开预候选的 Workshop 验收

PR #10 预候选 `08954a3c33d799e15c9ee9270fc578e10156379c` 的 Workshop 作业在托管 runner 上通过 81 项，0 失败、0 跳过。此记录对应一次公开预候选的 Workshop 作业。最终候选的完整 CI 与合并后的主线验收分别按各自提交签收。[作业与原始步骤](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43086830875/job/130753295169) · [检出日志、步骤和工件摘要](evidence.json)

| 原件 | 通过项 | 覆盖 |
|---|---:|---|
| [Workshop 工件](workshop-results.zip) | 62 | 48 个 Workshop 测试（含真实 Redis 容器停启）、5 个 NF 工具用例、2 个双服务用例、5 个 W0/W1/W2/w003/w003a 历史升级用例、2 个真实对端丢回执及双向物理 PITR 用例 |
| [NF 桥接工件](nf-bridge-results.zip) | 19 | 18 个 NF 委托、权限、进展及旧库用例，1 个 W2 员工与 NF 委托双身份 stdio 用例 |

GitHub 实际测试了合成提交 `c6c13d4f6b59c930114a9cda761d0c2e9a242323`。其双亲为已签收主线 `15f606c99c765deeffd904ad540441eb6476b3af` 与预候选，代码树与预候选同为 `2f4b472bd8685b48971fc0dd2075097765893c95`。按 Git 对象比较，预候选的 NF `src/`、`migrations/`、`tests/` 和 `runtime/` 与主线一致。相对前期预演采用的 `d8c4e970`，`workshop/` 仅多出已签收 NF 主线的 `acceptance/test_nf_bridge_stdio.py`；实际 W3 服务、迁移和队列实现相同。[分离 checkout 的独立预演](../preflight-d8c4e97-nf15f606c/README.md)

两个旧 W3 队列均经原发行代码写入后升级至 w003b。丢回执重连和两个物理 WAL 恢复位置均通过，恢复结果保留一项 NF 任务，商业模型调用为零。原件保持 GitHub 返回的 ZIP 字节和 SHA-256 摘要，JUnit 与 paired JSON 均在 ZIP 内。
