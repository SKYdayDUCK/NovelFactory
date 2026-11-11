# NF Bridge 独立验收原件

## 合并后主线复验

实测主线为 `15f606c99c765deeffd904ad540441eb6476b3af`，Git 树为 `788dc4b190299894509593a23982452f0362f9ef`。两份 ZIP 分别保存原始日志、JUnit、环境、代码版本和各自的复现说明。

| 原件 | 覆盖 | SHA-256 |
|---|---|---|
| [bridge-main-15f606c.zip](main-replays/bridge-main-15f606c.zip) | NF 委托、授权、S9 进度、升级、跨服务丢回执和双库物理恢复。含 16 项委托专项，7 个 JUnit 共 23 项通过。跨服务对端固定为 Workshop `ac579e0b89b55e613d32750ce1c07210e0b07022`。 | `1c81f917ede288387aff59db60966e9e6bebcaee09301af5ef630b07f9e8d0c8` |
| [w2-main-15f606c.zip](main-replays/w2-main-15f606c.zip) | 已部署 W0/W1/W2 升级、受限 NF 工具、路由、双服务和 Redis 停启。Workshop 主套件收集 35 项，其中 34 项通过，停机用例另在专用进程中实跑通过。两份 XML 均保留。 | `e17d8848d712b0c3e1b4aa5f59d1ecd6db3ce45843fdfd688d0273de0bb67c8d` |

## 合并前同宿主 1000 开环读对照

基线为已签收 W2 主线 `f2c584ec8e738e09d31b05ddd5c032bb2b8f96b1`，随后在同一宿主测 NF Bridge 候选 `695668b87f6a76acbe7e27c71fcb259b7b3f0795`。候选与合并提交同树。每臂使用独立 PostgreSQL 17.11 集群和两个独立 API 进程。每侧 1,000 个 Agent、5,000 次请求、目标 1,000 请求/秒、热点比例 0.8。保留原始验证、错误、阈值和退出码。

| 结果 | W2 基线 | NF Bridge 候选 |
|---|---:|---:|
| 经原校验的 HTTP 200 | 2,016 | 2,063 |
| HTTP 429 | 2,978 | 2,924 |
| 传输错误 | 6 | 13 |
| 实际 HTTP 请求头发送次数/秒 | 48.98 | 48.66 |
| 有效查询/秒 | 19.23 | 19.62 |
| 完整性验证错误 | 0 | 0 |
| 原容量门槛 | NOT_MET | NOT_MET |

单次基线后候选的测量保留了运行顺序和宿主负载记录。该对照没有重现此前跨 runner 的成功数下降。共享宿主活动、运行次序和缓存温度限制了因果解释。Issue #13 继续开放。

- [逐臂环境、容量结果、指标定义与文件哈希](w2-versus-candidate/comparison.json)
- [W2 全部 5,000 条原始请求](w2-versus-candidate/w2/original-result.json.gz) · [候选全部 5,000 条原始请求](w2-versus-candidate/nfbridge/original-result.json.gz)
- [测量入口和只读代码哈希](w2-versus-candidate/code-mapping.json) · [W2 SQL 形状](w2-versus-candidate/w2/query-trace.json) · [候选 SQL 形状](w2-versus-candidate/nfbridge/query-trace.json)

独立 SQL 核查安排在两个计时窗口之后。热点首读、热点重复读和冷点首读在两个版本中均为 17 条同序 SQL，规范化形状相同，没有访问委托表。每侧诊断调用使用 10 个生成的夹具身份中的选定身份。负载原件另存每个 API 进程实际采集的 SQL 数量和类型。

### 复现命令

准备两个独立的干净 checkout，分别检出上列 W2 和 NF Bridge 候选 SHA。将 `W2_CHECKOUT`、`CANDIDATE_CHECKOUT`、`PG_BIN`（PG17.11 二进制目录）、`TRACE_SCRIPT`（本目录下 `w2-versus-candidate/request-sql.py` 的绝对路径）和 `OUTPUT_DIR` 设置为绝对路径。执行用户须能在本机启动隔离的 PostgreSQL。每次调用都建立临时数据库和工作目录。

```bash
export PATH="$PG_BIN:$PATH"
unset NF_BENCH_DIAGNOSTICS
for arm in w2 nfbridge; do
  if [ "$arm" = w2 ]; then cd "$W2_CHECKOUT"; else cd "$CANDIDATE_CHECKOUT"; fi
  uv sync --locked --group dev --python 3.12
  mkdir -p "$OUTPUT_DIR/$arm"
  rc=0
  .venv/bin/python scripts/with_postgres.py --migrate --log-dir "$OUTPUT_DIR/$arm/pg" -- \
    "$PWD/.venv/bin/python" -m benchmarks.p3_open_arrival --agents 1000 --rounds 5 \
    --arrival-rps 1000 --hot-fraction .8 --api-processes 2 \
    --output "$OUTPUT_DIR/$arm/result.json" > "$OUTPUT_DIR/$arm/run.log" 2>&1 || rc=$?
  echo "$rc" > "$OUTPUT_DIR/$arm/exit.txt"
  test -s "$OUTPUT_DIR/$arm/result.json" || exit 1
done
for arm in w2 nfbridge; do
  if [ "$arm" = w2 ]; then cd "$W2_CHECKOUT"; else cd "$CANDIDATE_CHECKOUT"; fi
  PYTHONPATH="$PWD" .venv/bin/python scripts/with_postgres.py --migrate \
    --log-dir "$OUTPUT_DIR/$arm/diagnostic-pg" -- "$PWD/.venv/bin/python" \
    "$TRACE_SCRIPT" "$OUTPUT_DIR/$arm/query-trace.json" || exit 1
done
```

未满足原门槛时，基准完成原始 JSON 后返回 1。每个主线 ZIP 内的 `reproduce.md` 单独说明对应复验所需的 PostgreSQL、Redis 或 Workshop 对端。
