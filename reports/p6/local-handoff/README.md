# 在本地接手三书实验

这是 2026-11-13 北京时间 22:49:51 最后一笔 Worker 写入之后的停机快照。原宿主已停机，原定时守护已暂停。22:52:01 从停机副本导出最后一组观测。恢复默认离线执行，完成校验后关闭本地数据库。要继续产生付费请求，请单独启动选定作品的 Worker。

| 作品 | 写作 Work ID | 最新草稿汉字 / 历史版本 | 完整回执 | 接手状态 |
|---|---|---:|---:|---|
| A 公路灵异 | `e43e7ed0-9244-535c-b956-5fdb1185b408` | 2,490 / 67 | 176 | `STALE_GENERATION`，有一个暂停任务 |
| B 玄幻耽美 | `10d445a4-0bb2-5d41-84e6-155774fb2ff0` | 6,211 / 26 | 122 | 第 123 笔曾发出的请求缺响应，须对账 |
| C 原创无限流 | `c143087f-391e-5e7c-8fb1-ed218e88e691` | 4,163 / 35 | 200 | `RESULT_STORED`，可接续原结果 |

三书冻结章数均为 0。账本有 499 个调用 Attempt，498 份完整回执已知合计 5,219,004 Token，按本分支价卡估算为人民币 28.00268048 元。账本采用延后发票结算，完整传输和账单结算是独立状态。最终费用取决于供应商账单。

## 准备环境

使用 Linux 或 WSL2 的 Linux 文件系统和普通用户。需要 Git、Python 3.12、uv、zstd、PostgreSQL 17 的服务端和客户端工具。把同一套 17.x 的 `pg_config`、`initdb`、`pg_ctl`、`createdb`、`pg_restore` 放入 PATH。私有恢复文件 `NovelFactory-P6-recovery.json` 随交接单独下载。它包含解密材料和六个既有项目作用域的绑定，必须保留；DeepSeek Key 由你本地另行提供。

在同一个 Bash 终端执行以下命令。若已克隆仓库，可直接从实验分支的同一交接提交开始。

```bash
git clone --branch experiment/p6-three-novels-live https://github.com/SKYdayDUCK/NovelFactory.git NovelFactory-handoff
cd NovelFactory-handoff
handoff="$PWD/reports/p6/local-handoff/restore.py"
source_dir="$(dirname "$PWD")/NovelFactory-p6-frozen"
state="$HOME/novelfactory-private/p6-snapshot-20261113"
recovery="$HOME/Downloads/NovelFactory-P6-recovery.json"
git worktree add --detach "$source_dir" fffd0832c8e452f065efcd3cede7c62f234978bd
(cd "$source_dir" && uv sync --locked --no-editable --python 3.12)
py="$source_dir/.venv/bin/python"
chmod 600 "$recovery"
"$py" "$handoff" restore --root "$state" --source "$source_dir" --recovery-file "$recovery"
```

恢复需要一个全新目录和一个空闲回环端口，默认 55432，可用 `--port` 选择其他端口。脚本逐片核验 SHA-256，用 AES-256-GCM 验证和解密归档，再核验归档中的文件。它新建使用 SCRAM 密码的本地数据库，恢复事务，并把现行控制文件中的数据库连接和对象路径换成本地值。历史记录保留原值。整个流程只读源提交，生产程序会拒绝脏工作树或错误的源码 SHA。

成功时返回 77 张表、8,422 个登记对象和 A/B/C 各 176/122/200 份回执。结果写入 `$state/verified.json`。归档保存了 `pg_dump` 逻辑备份、8,423 个磁盘内容对象（含 1 个未登记对象）、完整传输回执及已捕获的响应头、原 Task/Context/Attempt/Supervisor、计划和正文版本、观察窗口以及停机日志。文件清单覆盖 9,281 个归档成员文件（清单自身除外）。原 PostgreSQL 登录密码、DeepSeek Key 和本机 GPT 登录凭据均未随包发布。

## 核对后启动

```bash
"$py" "$handoff" db-start --root "$state"
for book in A B C; do
  "$py" "$handoff" check-worker --root "$state" --source "$source_dir" --book "$book" || true
done
```

`check-worker` 只读账本。演练中，A 报告暂停任务 `7e0ccb55-6186-4e6e-b762-1128d96a4109`、代次 1；B 返回原 Attempt `399a5e51-a47f-4ae3-9111-655b68b082e6`；C 通过。对于 A，先检查该任务当前代次、暂停原因和上游回执，再通过现有 Supervisor 的恢复路径处理。对于 B，保留原 Attempt，凭客户端请求 ID `a3280c5f00a32768c61eaf42ff3a4d2400920d9b7f8f1fc99be176862afef7dc` 核对供应商执行情况。需要取得原响应，或取得明确未执行的证据并完成原账本对账。已提供的启动命令不会重新登记作品。请只在一个宿主接续这个快照。

准备仓库外的 `0600` 文件，格式为一行 `NF_DEEPSEEK_API_KEY=...`。下面的 Bash 命令以隐藏输入读取 Key，然后启动本次通过检查的 C。替换已有有效凭据文件时可直接跳到 `nohup` 行。SIGTERM 会通知原生 Worker 收尾，关闭数据库前请等它退出。

```bash
provider="$state/provider.env"
(umask 077; read -rsp 'DeepSeek API Key: ' key; printf '\n' >&2; printf 'NF_DEEPSEEK_API_KEY=%s\n' "$key" > "$provider")
nohup "$py" "$handoff" worker --root "$state" --source "$source_dir" --book C --provider-file "$provider" >"$state/C-worker.log" 2>&1 </dev/null &
worker_pid=$!
tail -n 5 "$state/C-worker.log"
```

停止这次启动的进程时，可在同一个终端运行以下命令。

```bash
kill -TERM "$worker_pid"
wait "$worker_pid"
"$py" "$handoff" db-stop --root "$state"
```

恢复目录长期保留数据库、对象和 Worker 状态。本机重启后用同一目录执行 `db-start`、`check-worker`、`worker`。最终全文独立评审需要本地有可用且已登录的 Codex 原生宿主。三书均未取得内部 RELEASE 或全文外审评分。本次迁移演练没有向模型发送请求。
