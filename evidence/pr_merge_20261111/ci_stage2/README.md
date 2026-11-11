# ci-stage2 可长期追溯的 CI 证据归档

冻结被测提交：008fccf23291c6f74693e450a88c224a436be7f6。本目录仅保存此历史检查点的实际终态证据；不把后续 HEAD、归档成功或工程绿色解释为容量通过。

覆盖 8 个终态 run、18 个 job、全部 18 个已下载 artifact 的摘要与 GitHub SHA256 校验映射。全部原始业务负载、全部开环/稳定性原始请求、四份 JUnit、实际可得的 PITR 文件、关键配对结果、执行源码快照和必要验收/恢复记录均原字节保存。没有最佳运行筛选；每次失败、429、transport error 和未恢复请求仍在原始 JSON 中。

archive_manifest.json 为逐文件原始/压缩 SHA256、长度、source run/artifact/member 映射。objects/ 用原始 SHA256 去重相同字节；同一对象的每个来源记录都保留，去重不删除任何请求行。index.json.gz 是原完整审核索引，overview.json.gz 是原精简数据，summary.md / analysis.md / checkout_tree_proof.json 同时保留可读原件。run_job_status.json 仅保留终态 run/job/step 状态和稳定 GitHub 链接。

## 核验与恢复

    python verify_archive.py
    python verify_archive.py --extract /new/empty/archive-restore

验证会重新检查每个 gzip 的 SHA256、解压字节数与原始 SHA256，以及可读摘要和控制文件哈希。恢复到新的目录并拒绝覆盖文件，不执行归档中的脚本。

## 边界

- stage2 的两套 PITR 原 artifact 各仅包含 8/11 清单项，另有 SHA256 清单；原 ZIP 也保留。缺失 pitr/archive_segment.py、pitr/restore.conf、pitr/source.conf，没有补造，也不宣称完整清单可恢复。stage3 没有执行独立 PITR、1000 开环或 600 秒 workflow，不能从别阶段补入冒充本次。
- 原始配对实验的全部运行均保留，包括 canonical 墙钟变慢的运行；归档不重算或提高验收状态。
- 每阶段省略 1104 个可再生成短卷文件、两份短卷日志和四份与 JUnit 重复的完整 JSON 测试报告。原始下载 ZIP/解包文件未删除或修改；完整原成员名、ZIP digest 与省略文件哈希仍在索引/manifest。此目录没有声称保存被省略的字节。
- 不保留签名下载 URL、授权材料或私有运行状态。请求/模型/世界信息来自已有合成测试证据；不开展新模型调用。
