# ci-stage4 可长期追溯的 CI 证据归档

冻结被测提交：e3c7b79b0058656f11257ea55edc75371382b73e。本目录仅保存此历史检查点的实际终态证据；不把后续 HEAD、归档成功或工程绿色解释为容量通过。

覆盖 6 个终态 run、18 个 job、全部 18 个已下载 artifact 的摘要与 GitHub SHA256 校验映射。全部20次50/200 Agent原始负载、四份JUnit、两份canonical/两份genesis/两份current-context结果、全部18份Actions job日志、执行源码快照与必要验收/恢复记录均原字节保存。没有最佳运行筛选；每次失败、429与未恢复查询仍在原始JSON中。另四个scale/PITR run由独立ci_stage4_scale归档，不在本目录冒充执行。

archive_manifest.json 为逐文件原始/压缩 SHA256、长度、source run/artifact/member 映射。objects/ 用原始 SHA256 去重相同字节；同一对象的每个来源记录都保留，去重不删除任何请求行。index.json.gz 是原完整审核索引，overview.json.gz 是原精简数据，summary.md / analysis.md / checkout_tree_proof.json 同时保留可读原件。run_job_status.json 仅保留终态 run/job/step 状态和稳定 GitHub 链接。

## 核验与恢复

    python verify_archive.py
    python verify_archive.py --extract /new/empty/archive-restore

验证会重新检查每个 gzip 的 SHA256、解压字节数与原始 SHA256，以及可读摘要和控制文件哈希。恢复到新的目录并拒绝覆盖文件，不执行归档中的脚本。

## 边界

- 本目录只有stage4的工程/P2矩阵/独立200六run；不把独立PITR、500 Agent、1000开环或600秒实验从其他目录混入。本目录也保留工程job内原有恢复、权限与验收记录，不能将其扩大解释为独立PITR或生产灾备SLO通过。
- 原始配对实验的全部运行均保留，包括 canonical 墙钟变慢的运行；归档不重算或提高验收状态。
- 本目录省略1110个成员，按原因实际计数为{'duplicate full JSON test report; JUnit preserved': 4, 'redundant generated short-volume material': 1106}。原始下载ZIP/解包文件未删除或修改；完整原成员名、ZIP digest与省略文件哈希仍在索引/manifest。此目录没有声称保存被省略的字节。
- 不保留签名下载 URL、授权材料或私有运行状态。请求/模型/世界信息来自已有合成测试证据；不开展新模型调用。
