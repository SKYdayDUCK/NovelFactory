# Stage4 PR3 scale/PITR 实际 CI 审核

提交：`e3c7b79b0058656f11257ea55edc75371382b73e`；源码树：`72328df65eafff795a5df1a10117c68c2d899bb6`。采集状态：**COMPLETE**。

## 实际运行状态

| Run | Event | Attempt | Status | Conclusion |
|---|---|---:|---|---|
| [42859219338](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219338) | pull_request | 1 | completed | failure |
| [42859219591](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859219591) | pull_request | 2 | completed | success |
| [42859202367](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202367) | push | 1 | completed | failure |
| [42859202666](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/42859202666) | push | 1 | completed | success |

## 500 Agent 混合业务

| Run | 正式提交 | 首试失败 | 容量 | 业务恢复 | 有效调用/s |
|---|---:|---:|---|---|---:|
| 42859202367 | 51/51 | 0 | PASS | PASS | 7.8696 |
| 42859219338 | 51/51 | 0 | PASS | PASS | 7.8765 |

## 1000 Agent / 5000 次只读

| Run | 有效成功/计划 | header-start/s | 429 | transport | 容量 |
|---|---:|---:|---:|---:|---|
| 42859202367 | 1181/5000 | 38.16 | 3731 | 88 | NOT_MET |
| 42859219338 | 1203/5000 | 38.49 | 3706 | 91 | NOT_MET |

## 600 秒探针

- 42859202367: 11708/12000 有效；call-start 20.0010/s；status={'200': 11708, '429': 292}；probe=NOT_MET；long_duration=NOT_EXECUTED。
- 42859219338: 12000/12000 有效；call-start 20.0009/s；status={'200': 12000}；probe=PASS；long_duration=NOT_EXECUTED。

## PITR

PR attempt1 的 curl56 安装失败及继发证据目录错误完整保留；恢复逻辑当时未执行。root 单次失败 job 重试产生单独的 attempt2 结果。

- 42859202666 / artifact 13785125246: PASS；manifest 11/11，hash全匹配=True；目标提交丢失=0；旧worker拒绝=True；原回执一致=True。
- 42859219591 / artifact 13784955992: PASS；manifest 11/11，hash全匹配=True；目标提交丢失=0；旧worker拒绝=True；原回执一致=True。

## 证据边界

- Only the specified e3c7b79 checkpoint is covered; later branch changes are not inferred.
- PR temporary merge 9fd27f6 and push head e3c7b79 have the same Git tree 72328df; actual job checkout logs are retained.
- Mixed harness git_dirty=true includes its generated benchmark source snapshot written before hardware() runs; the retained snapshot hash and recorded Git commit/tree are separately checked. No pristine full working-directory claim is made.
- PR PITR attempt1 failed on curl(56) downloading the PostgreSQL signing key before application installation or recovery. Subsequent evidence-directory/upload errors are secondary. Complete original failed logs remain retained.
- Root retried only that failed PITR job once without source/gate changes; attempt2 is separately retained and cannot erase attempt1.
- All downloaded artifacts retain complete ZIP bytes and all raw records. Every nonvalid/error row is separately indexed; no best-run selection.
- 500 mixed business commits, 1000 read-only throughput, and 600-second read-only stability are different gates; no substitution is made.
- Finished attempts are not valid successes. Wire/header-start is an HTTPcore event, not packet capture or proof of application acceptance.
- 600-second probe cannot satisfy the multi-hour production experiment; long_duration_gate remains NOT_EXECUTED.
- Two API processes share the runner with the generator and PostgreSQL. The scale harness does not launch a derived worker; combined API/worker resource capacity remains unmeasured.
- PITR is an isolated generated fixture with exact named-target recovery; its subsecond recovery measurement is not a production RTO promise.
