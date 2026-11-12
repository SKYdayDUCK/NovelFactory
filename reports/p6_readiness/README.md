# P6 小规模实验运行手册

以下命令在 WSL2 的 Linux 终端执行。默认操作能安装双服务、建立两本书、发布路由和登记流水线。实验预算模板为零，真实推理需要作者另行授权。运行结果和已知限制见 [验收矩阵](ACCEPTANCE.md)。每个实验保存 Git SHA、模板版本、工作及分支 ID、冻结路由、Attempt、费用、正文哈希和读者原表。

## 1. 在 Windows 上准备两个服务库

PowerShell 可用 `wsl --status` 和 `wsl -l -v` 确认发行版使用 WSL 2。已有 WSL2 发行版可直接使用，新机器可在管理员 PowerShell 执行 `wsl --install` 后按 Windows 提示重启。在 Docker Desktop 为该发行版启用 WSL 集成，或在 Linux 内使用 Docker Engine 和 Compose v2。进入 Linux home 下的工作树。私有绑定需要 POSIX 文件权限。

```bash
git clone https://github.com/SKYdayDUCK/NovelFactory.git
cd NovelFactory
git rev-parse HEAD
uv sync --locked --group dev
uv sync --project workshop --locked --group dev
export NF_ROOT="$PWD" NF_PY="$PWD/.venv/bin/python" AW_BIN="$PWD/workshop/.venv/bin"
export P6_PRIVATE="$HOME/novelfactory-p6"
umask 077
mkdir -p "$P6_PRIVATE"
export P6_SECRETS="$P6_PRIVATE/secrets"
python3 reports/p6_readiness/templates/wsl/make-secrets.py
docker compose -f reports/p6_readiness/templates/wsl/compose.yaml up -d --wait
```

私有口令只在首次建库时生成。后续启动保留该目录和数据库卷，重新设置 shell 环境即可。Compose 为 NF 和 Workshop 建立独立 PG17 实例、服务账号和数据卷。Redis 8 在本机另一个端口监听。示例用于单机实验。在分别运行 NF 和 Workshop 的账户中只配置对应服务的 DSN，NF 工作进程单独取得已授权模型的凭据。Windows 宿主、Docker Desktop 和真实签约账户的组合仍需在实验机验证。

```bash
export NF_DATABASE_URL="postgresql+psycopg://nf_service:$(cat "$P6_SECRETS/nf_password")@127.0.0.1:56431/novel_factory"
export AW_DATABASE_URL="postgresql://aw_service:$(cat "$P6_SECRETS/aw_password")@127.0.0.1:56432/agent_workshop"
export NF_OBJECT_ROOT="$P6_PRIVATE/nf-objects"
export NF_REDIS_URL="redis://:$(cat "$P6_SECRETS/redis_password")@127.0.0.1:56379/0"
export AW_REDIS_URL="redis://:$(cat "$P6_SECRETS/redis_password")@127.0.0.1:56379/1"
"$NF_PY" -m alembic upgrade head
"$AW_BIN/aw-migrate" upgrade
"$NF_PY" -m alembic current
"$AW_BIN/aw-migrate" current
```

升级现有部署前保存 NF 对象目录、两侧数据库备份、安装映射和进行中的 Attempt。先升级恢复副本并检查原任务、预算、能力绑定。已有安装和管理者可跳过下面的首次登记。

```bash
INSTALL_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
NF_INSTANCE=$(python3 -c 'import uuid; print(uuid.uuid4())')
ORG_ID=$(python3 -c 'import uuid; print(uuid.uuid4())')
"$AW_BIN/aw-admin" install "$INSTALL_ID" "$NF_INSTANCE"
"$AW_BIN/aw-admin" bootstrap "$ORG_ID" --preset Minimal --installation "$INSTALL_ID"
"$AW_BIN/aw-admin" enroll "$INSTALL_ID" "$ORG_ID" manager "$P6_PRIVATE/manager.json" --revision 1
```

Workshop 的管理面可用 `aw-mcp --binding "$P6_PRIVATE/manager.json"` 通过 stdio 接客户端。委托真实动态员工时，按已有的[双服务运维步骤](../../workshop/docs/operations.md)完成 NF 安装映射、允许槽位、SSH 启动器和经理定时器。产品中的两侧数据库用户各自只能访问其服务库。

## 2. 配置 Codex、DeepSeek 和预算

在 WSL 的 PATH 中安装 Codex CLI，执行 `codex --version` 记录二进制版本。复制模型目录，把两个 `REPLACE_WITH_...` 改成账户实际有权运行的模型 ID。示例的 Codex 路线使用真正的 app-server，配合 OpenAI API Key 和一次受控的第一方 Responses 请求。DeepSeek 路线使用独立的 Chat Completions 接口。两种 API 分别计费。普通桌面 Codex 登录本身不能证明该实验已经取得订阅额度或产生模型结果。授权和实际费用需要从该账户核实。

```bash
cp -n reports/p6_readiness/templates/providers.json "$P6_PRIVATE/providers.json"
export NF_RUNTIME_PROFILES="$P6_PRIVATE/providers.json"
# 在私有目录编辑 models；仅在有授权的服务进程中通过秘密管理器或静默输入注入密钥
"$NF_PY" scripts/P6_profile_preflight.py --profiles "$NF_RUNTIME_PROFILES" > "$P6_PRIVATE/model-inventory.json"
```

预检仅检查本地配置，退出码 2 表示缺少模型或凭据，仍会生成 JSON。无网络握手，不产生费用。Codex 和 DeepSeek 凭据名称分别由模板中的 `secret_ref` 指定。部署者可在私有 shell 中使用 `read -rs NF_OPENAI_API_KEY; export NF_OPENAI_API_KEY` 和 `read -rs NF_DEEPSEEK_API_KEY; export NF_DEEPSEEK_API_KEY`。密钥值不进入命令行参数、模板、Git 或角色消息。保留 `NF_RUNTIME_LIVE_APPROVED` 未设置直至第 4 节。

使用 ChatGPT 计划额度必须有为本应用登记的 Sign in with ChatGPT 客户端。可改从 `templates/providers-plan.example.json` 开始。填写 OAuth client ID 和授权模型，从本应用正常授权流程取得访问令牌，并由服务身份注入 `NF_CODEX_PLAN_TOKEN`。模型条目中的两个上限 `null` 是未填写的占位，必须改为该模型文档确认的**完整输出硬上限**并相应配置每次预留。该计划接口不接受每次请求的 max_output_tokens。缺少已确认的模型上限时请保持该路线停派。运行时校验官方签名、令牌的 issuer、audience、client_id、scope 和有效期。API Key 不具备这些声明。仅在真正调用成功后记录访问资格，已结算费用仍由独立账单证明。[官方令牌说明](https://developers.openai.com/siwc/token-sharing-open-source/token-reference)

将 `templates/book.json` 分别复制为 `$P6_PRIVATE/book-A.json` 与 `book-B.json`，填写书名、角色、章细纲、未来卷前提。预算字段包括币种、全书金额、单次金额预留、全书 Token 和单次 Token 预留。离线建书可直接用零预算模板。计划真实运行的作品必须采用作者针对该作品确认的金额和 Token 上限。每次预留还需容纳冻结输入及最大输出。预算属于特定作品，跨作品资金不能互换。

## 3. 创建两本书并采用各自的路由

以下假设已安装 jq。Bootstrap 以私有目录作为恢复点，重复执行可核对原作品。每个目录持有独立控制凭据、作品与分支、第一卷有效细纲、预算和生产请求。

```bash
for BOOK in A B; do
  D="$P6_PRIVATE/$BOOK"
  "$NF_PY" scripts/p58_bootstrap_book.py --book "$P6_PRIVATE/book-$BOOK.json" --private-dir "$D"
  ACCESS="$D/supervisor.json"
  WORK=$(jq -r '.work_id' "$ACCESS")
  BRANCH=$(jq -r '.branch_id' "$ACCESS")
  "$AW_BIN/python" -m agent_workshop.policy_cli --binding "$P6_PRIVATE/manager.json" publish \
    --work "$WORK" --branch "$BRANCH" --expected-revision 0 \
    --rules reports/p6_readiness/templates/routing-rules.json > "$D/policy.json"
  "$NF_PY" scripts/runtime_policy.py --access-file "$ACCESS" activate --policy "$D/policy.json" --expected-revision 0
  "$NF_PY" scripts/runtime_policy.py --access-file "$ACCESS" preview --role writer --kind scene_draft
  "$NF_PY" scripts/runtime_policy.py --access-file "$ACCESS" preview --role volume_director --kind window_plan
  "$NF_PY" scripts/runtime_limits.py limit work "$WORK" --slots 2
  "$NF_PY" scripts/runtime_limits.py weight "$WORK" 1
  "$NF_PY" scripts/run_supervisor.py --access-file "$ACCESS" --register-pipeline "$D/pipeline.json"
  "$NF_PY" scripts/run_supervisor.py --access-file "$ACCESS" --pipeline-status > "$D/before.json"
done
```

默认配置让 DeepSeek 写正文、Codex 规划下一卷和阅读已完成的章节。可为两本书发布不同的管理策略。`preview` 会显示具体命中规则，仅供管理员。用 `runtime_limits.py` 的 `limit provider` 和 `limit account` 可另设共享账号槽位、RPM、TPM。实例覆盖、代际冻结、更换服务商、已发送 UNKNOWN 和预发送重绑的命令见[模型路由手册](MODEL_ROUTING.md)。跨卷计划、局部失效、正式接收和稿件导出见[生产流水线手册](PRODUCTION_PIPELINE.md)。

## 4. 经授权后开始小规模实验

在自己的管理记录中登记作品 UUID、模型账户及授权方式、每本书预算、每次上限、允许的窗口和停止条件。查看两个登记回执并确认有足够预留。打开两套 NF 工作进程时，在各自的私有服务环境仅保留所需凭据。以下为已明确授权后才运行的命令。

```bash
export NF_RUNTIME_LIVE_APPROVED=1
"$NF_PY" scripts/run_supervisor.py --access-file "$P6_PRIVATE/A/supervisor.json" --workers 2 > "$P6_PRIVATE/A/worker.log" 2>&1 &
PID_A=$!
"$NF_PY" scripts/run_supervisor.py --access-file "$P6_PRIVATE/B/supervisor.json" --workers 2 > "$P6_PRIVATE/B/worker.log" 2>&1 &
PID_B=$!
```

初始 PostgreSQL 调度模式即可运行本示例。需要 Redis 唤醒传输时按 [P5.5 安装与运维](../../docs/implementation/P5.5_安装与运维.md)启用 relay、消费组与各作品执行器。每个作品的 `--pipeline-status` 可核对节点、Attempt 和等待原因。`--pipeline-results` 导出该分支已返回的正文、审稿意见和前瞻候选。只有第一卷正式连续性结算后，匹配的下一卷候选才能交给正式规划入口。

完成小规模窗口后停止本次工作进程。在途请求先按原 Attempt 对账；强行终止进程时，账本可能保留 UNKNOWN。清除父 shell 的授权环境不会撤销已启动子进程中的环境。

```bash
kill -TERM "$PID_A" "$PID_B"
wait "$PID_A" "$PID_B" || true
unset NF_RUNTIME_LIVE_APPROVED
"$NF_PY" scripts/run_supervisor.py --access-file "$P6_PRIVATE/A/supervisor.json" --pipeline-status > "$P6_PRIVATE/A/after.json"
"$NF_PY" scripts/run_supervisor.py --access-file "$P6_PRIVATE/A/supervisor.json" --pipeline-results > "$P6_PRIVATE/A/writing.json"
```

## 5. 核对并行、成本和文学评估

从同一本书选择两个不同角色的 Attempt UUID。导出器逐个校验作品及分支归属。它只把两项真实适配器的有回执 HTTP 发送至接收时间重叠记为 OBSERVED。模型协议夹具、两个进程同时存在、无供应商回执和仅有账本预留都不能产生此结果。这是客户端观察到的调用重叠，供应商内部 GPU 调度不可见。

```bash
"$NF_PY" scripts/p6_provider_evidence.py --binding "$P6_PRIVATE/A/control.json" \
  --attempt "$ATTEMPT_WRITER" --attempt "$ATTEMPT_DIRECTOR" > "$P6_PRIVATE/A/provider-receipts.json"
```

每行留存 Attempt、代际、账本状态、原响应哈希、供应商回执、用量和已结算金额。输出缺失或请求超时时按原请求号向供应商查账。已有原始响应、可匹配的逐请求发票和精确用量时，作品全域预算管理员可用下列命令登记已核对的实际金额。CLI 校验回执、币种、用量、上限，并记录原账单文件 SHA-256。同一请求重复导入相同凭据只结算一次。仅有订阅定额、汇总余额、单价估算或缺少逐请求费用时保留 null。

```bash
"$NF_PY" scripts/p6_reconcile_invoice.py --binding "$P6_PRIVATE/A/control.json" \
  --invoice "$P6_PRIVATE/A/original-invoice.pdf" --attempt "$ATTEMPT_WRITER" \
  --receipt-id "$PROVIDER_RECEIPT" --currency "$INVOICE_CURRENCY" \
  --amount "$CONFIRMED_REQUEST_AMOUNT" --tokens "$CONFIRMED_TOTAL_TOKENS"
```

工程测试中的原生文学审核结果记录阅读资格、证据和编辑决定。真实文学验收另取同一冻结正文的 SHA-256，由两名独立读者分别阅读并填写[独立阅读表](../handoff/P6_READER_FORM.md)。事先保存匿名材料、原始意见和分歧，按[冻结与导出流程](../../docs/P6_本地真实模型创作交接.md)复核。未取得真实正文和独立评分期间维持 NOT_EVALUATED。

供应商接口参考：[Codex app-server 和 ChatGPT OAuth](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server) · [计划账户的请求字段限制](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations) · [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)。
