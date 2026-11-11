# CI历史配对实验兼容调整

## 原因与方法

`benchmarks/context_paired_profile.py` 是冻结的历史实验：其baseline query来自 `0609f680fad782c827dd3834a073f8f9bcddaf0d`，同时逐字节断言kernel/world等6个shared core文件仍与0609相同。原实验用于分离上下文依赖SQL批量化与哈希复用的效果。此次kernel/world合法性能优化会使该研究前提不再成立；把source_checks删除、改成宽松比较或换baseline，会改变原实验并丢失可复现性。

本次只修改 `.github/workflows/p1-integrity.yml`：

1. 当前HEAD原有完整pytest步骤字节不变。
2. 新增当前HEAD `python -m benchmarks.genesis_paired_profile --agents 200 --repeats 5 --output ci-evidence/genesis-paired.json`。已与backend_perf协调CLI。它比较固定e0b原replay与当前实现的同一真实200Agent数据库输入，保留完整snapshot相等和世界状态/身份/scope版本不变核验；收益/退步均实报，不设虚构性能PASS门槛。
3. 原Context历史实验完整移动到固定 `e0b0f2b080f3225d8be8e202e13258bc480f5b84` detached worktree执行，参数仍1000实体、12历史、1/20/100主体、5对，使用当前CI已锁依赖的 `.venv/bin/python`，固定PYTHONPATH只含历史src/root，并从历史目录启动。
4. 原0609源码断言、完整facts/evidence/guard比较、世界不变断言均原样执行，任一失败仍令job失败；未改原脚本、core、baseline或退出码。
5. 为防editable安装误导入当前源码，先验证paired/kernel/world的实际 `__file__` 均属于固定历史worktree，再执行其原 `source_checks()`。结果另写method.json，带historical SHA、workflow checkout SHA、source hash、当前依赖锁hash与 `HISTORICAL_REPRODUCTION_NOT_CURRENT_HEAD_VALIDATION` 标记。
6. 历史输出独立放 `ci-evidence/context-historical-e0b0f2b/`，不与当前 `genesis-paired.json` 混淆。现有Artifacts总目录和失败保留规则不变。
7. 现有 `p3_context_comparison.py` 未改；其在 `p3-p4-evidence.yml` 对当前core执行完整事实/guard配对的步骤保持不变。当前全套pytest、memory服务探针、短卷与负载smoke等也保留。

## 验证范围

- YAML实际解析通过；PostgreSQL job所有run脚本通过 `bash -n`。
- 嵌入的metadata Python语法解析通过。
- 逐字节比较确认原当前HEAD全套pytest命令未变。
- 已建立本地固定e0b detached worktree，使用当前 `.venv/bin/python`＋相同固定PYTHONPATH实际import核对通过，原 `source_checks()` 全部通过。原paired脚本SHA-256为 `e6f34c7001c6c67d1ef4da939ad110a463b7552ad107ba6e50a31c433518acfa`。
- 静态结果保存 `/workspace/scratch/f6d7dc8c770b/context_history_static_validation.json`。
- `git diff --check`通过。
- 未启动数据库、服务或负载；完整历史实验及当前新genesis测量需在正式CI运行后记录真实结果。新genesis脚本由backend_perf交付，必须与workflow一起上传。

## 风险与解释限制

历史step成功只能说明固定历史实验可复现，不能作为新HEAD的内核验证。当前新优化由新配对实验、当前完整测试、当前core的上下文事实/guard检查及实际容量实验共同验证。当前虚拟环境依赖锁被记录；未来升级依赖后可能影响历史实验，应保留差异而不是宣称原环境完全相同。固定worktree和原postgres service共享机器，但只用于工程机制测量，不是跨环境容量对照。新增step没有降低任何容量阈值，容量工作流/失败门槛/超时/重试/池设置均未在此改动。
