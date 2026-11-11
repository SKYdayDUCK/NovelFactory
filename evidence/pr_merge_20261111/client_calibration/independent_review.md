# PR3 客户端校准与 canonical 只读复核

审阅对象：`NovelFactory-pr3/benchmarks/client_calibration.py`、`benchmarks/p3_open_arrival.py`、`evidence-local/client-calibration/{calibration.json,summary.json,README.md,executed_client_calibration.py}`，以及当前 staged `canonical.py`、冻结参考源码和差异性质测试。未重跑负载，未修改仓库源码或原始证据。

## 结论

本次审阅未发现阻断缺陷。该工具与结果严格限于 `GENERATOR_CALIBRATION_ONLY`；NovelFactory 容量和业务提交均为 `NOT_APPLICABLE`，不能进入原业务或容量通过判定。四场 197.42、203.65、212.01、241.31 header-start RPS 的实测表明，在这组共享宿主、CPU 预算、HTTPX 调度与连接池条件下，生成器对最简固定字节服务器也没有提供 1000 次/秒请求头开始事件。该观察不能证明 NovelFactory 的服务容量上限，也不能排除服务端瓶颈。

## 已独立核对

- `p3_open_arrival.py` 与当前 HEAD 的文件字节相同，SHA256 为 `515ec1ec61e03317cd6f160a22a309aa38bc6df8bbfd4604e3dacfa43b14e9da`。调用未经修改的 `offer`，1000 个合成客户端、5 轮、5000 个请求、计划 1000 RPS、256 连接、30 秒超时，不重试、不删行。
- 20,000 条原始记录全部复核：每场 index 为 0–4999，无缺失和重复；每 Agent 恰好 5 次；计划时间为 index/1000；计划、dispatch、header-start、完成时间顺序正确；重新计算 header-start RPS 与记录一致。
- 每场全部 5000 个 HTTP 200 的 body 长度、SHA256、匹配标记一致；客户端与诊断服务器请求数、响应数、请求 body 总字节数、响应 body 总字节数完全对账；无 handler error 或断连。校准结果删除 `valid` 和原始 `_content`，未将固定回包计为领域响应有效。
- 2104 字节响应来源的原始 CI JSON SHA256 为 `653430c3d23574cb784fbd017a573e3b54a23362cf7bb140877c627039761bc1`。实际筛选 `label=overload,status=200` 得到 916 条，响应大小均为 2104。64 字节档被明确标为乐观传输参考。
- header-start 的具体边界是 HTTPcore `http11.send_request_headers.started`，发生于连接获取之后、发送请求头开始之前。它不是抓包、完整请求 body 已发出、服务端接受或业务完成事件。脚本和说明保留了此限制。
- CPU 以独立进程 `RUSAGE_SELF` 分别统计；客户端 affinity `[6,7]`、诊断服务器 `[8]`，比例明确按一个 CPU 核的时间计算，不是占 2/3/9 核预算的百分比。约 99.80%–99.90% 与约 6.10%–7.90% 的计算和原始数据相符。
- 原始 JSON SHA256 `ec6e278c09e76476e3f54bcd89b8a576a0ca9edc0216d57d8ad6798ca2c641cb` 与摘要一致。实际执行源码 SHA256 `b915bcb119bc38fdd8271ffd58b781fb96b0443473b71052ce18af8c279137a4` 与原始 JSON 一致。当前源码 SHA256 `d9d11101c80b75c58e6cce69199285574cff3e60c4cc82bd0cd46ec9fb1dfdf5` 仅更正从 P2 hardware() 继承的静态拓扑和 helper hash 字段，测量代码与 offer 不变。

## 必须随证据保留的解释

原始 JSON 的 `load_client_location/api_processes/api_server_threads` 误继承 P2 通用静态值；原始记录未被覆盖，执行源码快照、摘要与 README 的勘误共同说明真实独立诊断进程。发布时应保留这组说明，不能单独引用原始错误拓扑字段。执行 HEAD 为 `008fccf23291c6f74693e450a88c224a436be7f6`，当时 `git_dirty=true`；未提交校准工具的真实身份由执行源码快照哈希约束，不能称为该 HEAD 已提交文件。

客户端 CPU 差值包含 `asyncio.run` 和请求编码，`network_window_seconds` 从 offer 内编码之后开始；诊断服务器 CPU 取样从 ready 前到 snapshot，边界也略宽。故 CPU/窗口是近似负载描述，不能当作严格同窗口利用率。RSS 是进程生命周期高水位，不能作为单轮增量、瞬时常驻或内存泄漏结论。所有数据仅为短窗口共享宿主观察。

## canonical 最终补丁

当前 `src/novel_factory/domain/canonical.py` SHA256 为 `019ad4d94016914ede2c6a4750a4a033076cb69b81b89873ec4d218ffb506e51`。变更仍只缓存局部 `type(value)` 判定并内联严格字符串 key 的 UTF-8 检查；精确原生类型、浮点/custom 拒绝、安全整数范围、Unicode 拒绝、字典 key-before-value 顺序、列表顺序、JSON 编码和指纹保持原义。未引入 schema memo 或跨请求权限/状态缓存。

冻结参考文件 SHA256 `3fc335ad00e3973b200683e953e5066a64ce6f0040035df000c3dfc350fa1571` 独立复核一致。差异测试覆盖 500 个确定性 Hypothesis 样例、原生类型子类、非法 key、自定义对象、Unicode surrogate、安全整数边界、多个错误的优先顺序、严格 JSON loader 重复 key/浮点/非法编码，并比较完整错误封装。此次为源码只读复核，不新增执行或性能通过主张；未发现实质缺陷。
