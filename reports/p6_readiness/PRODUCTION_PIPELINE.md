# 在 WSL2 建立作品并启动分卷、分章生产

在安装好 NovelFactory、完成数据库迁移的 WSL2 发行版内执行下列命令。使用 Linux 用户的私有目录，使作者的数据库连接、管理凭据和未发布正文都只对当前用户可读。示例使用已经激活的仓库 Python 环境。

## 从作者细纲建立一本书

`templates/book.json` 有书名、人物、地点、第一卷细纲、未来卷创作意图、每阶段模型路由和共享预算。默认金额上限为零。保留零上限即可练习建书和登记。要运行经授权的真实实验，请在创建作品前填写授权书规定的总金额、币种、单次预留、总 Token 和单次 Token。预留金额只是账本围栏。实际金额以供应商回执和后续结算为依据。

```bash
umask 077
PRIVATE="$HOME/nf-p6"
mkdir -p "$PRIVATE"
cp reports/p6_readiness/templates/book.json "$PRIVATE/book-a.json"
# 编辑 book-a.json，填写本次作品和授权范围
python scripts/p58_bootstrap_book.py --book "$PRIVATE/book-a.json" --private-dir "$PRIVATE/a"
ACCESS="$PRIVATE/a/supervisor.json"
python scripts/p6_budget.py --binding "$PRIVATE/a/control.json" show \
  --budget-id "$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["budget_id"])' "$PRIVATE/a/pipeline.json")"
```

构建器在一个数据库事务里建立独立作品、接受的第一卷计划、写作批次、每章一个场景卡和第二卷候选契约。它在私有目录写入 `supervisor.json`、`control.json` 和 `pipeline.json`。相同书籍配置、数据库和目录可以安全重复执行。这个起步模板为每章建立独立的场景；复杂编排可由相同原生规划与写作服务创建多个场景和前序交接。将自己的第二本书写入 `book-b.json` 并改用 `--private-dir "$PRIVATE/b"`，会得到独立的作品和分支 UUID、管理员凭据、预算及生产文件。

分别按 [模型策略和热切换](MODEL_ROUTING.md) 为每本书采用策略并核对预览。附带的路由规则将正文交给 DeepSeek，将卷纲和早期阅读交给 Codex。注册阶段不调用模型。若有该作品的工作进程已经运行，注册完成后它们即可领取工作，因此请先确认真实实验已获授权，再向运行中的工作进程注册。

```bash
python scripts/run_supervisor.py --access-file "$ACCESS" --register-pipeline "$PRIVATE/a/pipeline.json"
python scripts/run_supervisor.py --access-file "$ACCESS" --pipeline-status
```

## 并行写作、前瞻规划和章节审核

以下命令会执行已登记的模型任务。只有获得实际服务调用授权后才启动。本例使用 PostgreSQL 准入；每个作品启一个 supervisor，保留部署设定的全局及作品容量限额。

```bash
python scripts/run_supervisor.py --access-file "$PRIVATE/a/supervisor.json" --set-transport postgres
python scripts/run_supervisor.py --access-file "$PRIVATE/a/supervisor.json" --workers 2 --interval 1
```

在另一个 WSL 终端设置相同运行时变量，将路径改为 `$PRIVATE/b/supervisor.json` 可同时执行第二本书。关闭并重新启动 supervisor 会从数据库恢复。若只需走一步，可用 `--once` 查看一个调度循环产生的回执。该选项在有待领取任务时也会调用模型。

第一卷作者已采用的细纲、当前场景卡、批次与前序正文接受回执共同限定写作输入。正文角色只收到自己的写作卡。前瞻节点单独领取 PLAN 任务，其依赖锚定到具体计划节点指纹或服务器验证的世界条件。同一章所有场景的实际模型正文存入原生草稿之后，即可向单独的评审身份送交这一章。审核积压达到 `limits.review_backlog` 后，停止新增正文和前瞻派工，保留已经执行的任务。每阶段自己的 WIP 上限、作品容量和预算继续约束派工。

```bash
python scripts/run_supervisor.py --access-file "$ACCESS" --pipeline-status > "$PRIVATE/a/status.json"
python scripts/run_supervisor.py --access-file "$ACCESS" --pipeline-results > "$PRIVATE/a/results.json"
```

`status.json` 含每个节点的任务 ID、Attempt ID、登记/完成时间、当前状态及引用。将两个实际 Attempt 传给 `scripts/p6_provider_evidence.py --binding "$PRIVATE/a/control.json" --attempt ... --attempt ...` 可复核传输时间是否重叠、输入输出 Token、账本状态和已结算金额。原始凭据保留在控制文件中。结果导出包含可以交给独立读者的精确草稿、文本哈希、模型阅读意见和候选卷纲。正式对账、审核、正文接受、冻结和跨卷发布继续使用原生服务。报告的人类读者评分保持 `NOT_EVALUATED`，直至另行收回真实读者评价。

## 结局变化和本地修订

| 节点状态 | 依据与可继续操作 |
|---|---|
| `PROVISIONAL_WITH_ASSUMPTIONS` | 已验证的候选保留在自己的 Attempt 上，不选择正式计划。等待该系列的前卷连续性在当前分支 HEAD 发布。 |
| `READY_FOR_ADOPTION` | 已发布快照包含正确的直接前卷，并且候选的每个世界条件和源计划节点指纹在复验中成立。 |
| `REPLAN_REQUIRED` | 某条依赖被推翻或证据不足。同章草稿及其他候选保持原哈希。 |
| `WINDOW_TRANSFERRED` | 作者显式将已通过再验证的候选转为当前切面的原生 DRAFT 窗口，可以进入常规计划审批。 |
| `STOPPED` | 详见 `reason` 和相应的 NF 任务及 Attempt。可能是过期、授权失效、上游失败或需对账。 |

`--transfer-window` 接受含 `world_ref`（已发布且仍为当前 HEAD）、`candidate_id`（去掉 `plan:`）、`idempotency_key` 的 JSON 文件。执行时会再次检查当前快照和每条依赖。成功后，原候选和旧切面可继续查询。

`--revise-window` 在同样三个字段之外接受 `expected_candidate_ref` 和新的 `assumptions`。它将一个已被判定失效的候选排回队列，由新的 NF 任务基于新切面执行。用完全相同的幂等键重试可取回回执。每个候选每轮最多登记两次局部修订。原候选任务、结果、原因和取代关系保存在该节点的 `history`。在运行中的 supervisor 可立即领取新修订。

每轮最多两卷前瞻、八个条件化候选、一百二十八个节点。登记文件的 `limits.lookahead` 可收紧为一卷。世界依赖支持已有 PlanPrecondition 的 `ENTITY_ACTIVE`、`STATE_EQUALS`、`RESOURCE_AT_LEAST` 和 `EVENT_SELECTED`。文学承诺、披露、风格及角色认知相关的依赖可绑定到作者指定的源计划节点，记录该节点的完整指纹。这类边追踪作者对计划的修改，未读取角色私有认知。存在未确认的模型请求时，系统会阻止以新一轮登记重复下发，保留原款项并等待对账。
