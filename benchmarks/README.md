# R1 负载模型与后续压测资产

这里是**负载输入、计算示例和待实施压测说明**。它不包含小说工厂服务，不会发网络请求、运行SQL或调用付费模型。

## 文件

| 文件 | 用途 |
|---|---|
| [workload_model.json](workload_model.json) | 角色比例、思考/空闲周期、请求阶段、SQL扇出、缓存、数据尺度、热点和预算假设 |
| [calculate_load.py](calculate_load.py) | 标准库计算器；角色取整、流量/SQL/token需求、固定种子到达计数及闭环敏感性 |
| [load_model_result.json](load_model_result.json) | 本轮实际计算输出，状态为MODEL_CALCULATED_NOT_SERVICE_BENCHMARK |
| [load_model_table.csv](load_model_table.csv) | 5档人口×3缓存假设的15行计算表；空测量列表示未测量 |
| [15章](../docs/15_并发压测与端到端验收.md) | 真实压测流程、指标、A–H案例和扩展门槛 |

## 复现

```bash
python benchmarks/calculate_load.py
```

可通过 `--config` 与 `--output-dir` 指定新的输入和独立输出目录。默认配置固定seed=20261110；脚本保存输入SHA-256、Python版本和计算范围。复现时比较输入哈希及数值结果；它们不是服务性能数字。

输入中的state_query/history_query/graph_query等是**流量类别**，不是声明新增同名MCP工具。实际驱动需映射到统一查询/批量/上下文接口。一个批量请求的工具次数、内部查询项、SQL扇出与返回字节数分别计数。Context Compiler读取cache时仍有当前权限与版本检查；模型把这些查询列为uncached_reads。

`canonical_commands_per_cycle` 是联合裁决/维护形成控制面命令的平均机会，不等于角色可以直接提交，也不等于接受成功率。真实重试、拒绝、证书重新准备、计划失效和外部调用UNKNOWN均会改变需求，须从跟踪数据补入。

## 真实驱动尚待实现的接口

P1/P2可选k6或Python异步驱动调用已实现HTTP/MCP适配器，复用相同业务用例。负载驱动必须保存：scheduled_at、sent_at、completed_at、结果类别、请求/任务/命令ID、world_ref、payload_hash、重试父请求，以及与权限相容的证据。不能在日志中存秘密正文或原始凭据。

行为闭环维持角色依赖；open-loop按计划到达发出请求并记录不能发出的请求；两者不能共用一个“完成一个才再发一个”的循环后声称到达率恒定。负载机自身CPU、连接和未发出量也必须监测。

服务端性能结果另存新运行目录，状态应为MEASURED，包含冻结环境、实际吞吐/延迟/资源/成本与正确性证据。禁止手工把本目录计算结果的状态改成MEASURED。
