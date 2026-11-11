# P6 真实创作运行记录

本次执行了仓库已有的无请求路由预检，现场回执见 [P6_ROUTE_PROBE.json](P6_ROUTE_PROBE.json)。项目没有提供经授权的作品和分支绑定，也没有为首次推理指定供应商、金额上限、Token 总额和每次输出限制。生产模型请求数为零，尚未取得小说正文、供应商账单或独立文学评分。

## 启动现场

1. 在隔离服务账户中读入 NF 服务配置，固定已验收的 Git SHA、PostgreSQL 与 Redis 版本、作品与分支、对象备份、初始 WorldRef、角色 KnowledgeCut 和审核人身份。
2. 执行 `uv run --no-sync python scripts/probe_model_environment.py --output p6-route.json`，检查回执中的 http_route。作者确认价卡、币种、总金额、每次预留、Token 总额和单次最大输出。
3. 按[真实场景运行手册](https://github.com/SKYdayDUCK/NovelFactory/blob/b3b0e9b08a4ef2160a70fd858ca64de1fd58b6a5/docs/P6_本地真实模型创作交接.md)登记已授权的 3–5 个角色、初始场景及后继窗口。事先标出密信被证伪影响的任务和无关的远方场景。
4. 首场与后继验证通过后按同一实验扩展至 10–20 场。逐场保存 NF Task/Attempt、KnowledgeCut、变更或冲突案件、费用回执、正式接受的正文和 WorldRef。UNKNOWN 保留原 attempt 与费用预留直到供应商对账。
5. 冻结匿名正文交给两个独立读者，分别保存因果、角色认知、动机、文风、可读性的评分、例句及分歧。当前没有分配评阅人。

## 可复核的离线首场预检

模板 [P6_experiment.example.json](P6_experiment.example.json) 记录实验、已接受计划、S9 预标注证据和消费授权。模板 [P6_scene.example.json](P6_scene.example.json) 使用现有 `run_supervisor.py --register-scene` 的实际 JSON 格式。两个模板保留空值，防止误用合成作品或随意设定金额。作者在私有实验目录填好它们。服务账户从原有私有环境向适配器提供凭据，两个 JSON 均不接收凭据。

以下命令使用已安装 NF 锁定依赖的 Python。`P6_TOOLKIT` 指向包含本预检的工作树，`NF_CHECKOUT` 指向部署的 P5.5 或后继 NF 工作树。`P6_PRIVATE` 指向本次实验的私有目录。实验填写实际部署 SHA 和 Skill 目录 Git tree。后者覆盖目录内所有文件，可用同一源码逐字节复核。当前登记入口从首场时间和每个参与者生成自己的 BELIEF KnowledgeCut。填写角色与时间后，正式运行仍需保留实际切面回执。

```sh
umask 077
mkdir -p "$P6_PRIVATE"
cp -n "$P6_TOOLKIT/reports/handoff/P6_experiment.example.json" "$P6_PRIVATE/experiment.json"
cp -n "$P6_TOOLKIT/reports/handoff/P6_scene.example.json" "$P6_PRIVATE/first-scene.json"
git -C "$NF_CHECKOUT" rev-parse HEAD
git -C "$NF_CHECKOUT" rev-parse HEAD:runtime/skills
cd "$NF_CHECKOUT"
uv run --no-sync python "$P6_TOOLKIT/scripts/P6_preflight.py" \
  --nf-checkout "$NF_CHECKOUT" --experiment "$P6_PRIVATE/experiment.json" \
  --scene-file "$P6_PRIVATE/first-scene.json" --output "$P6_PRIVATE/preflight.json"
```

指定的实际供应商端点和模型必须与后端配置相同。金额使用非负十进制字符串。首场每次金额预留及 Token 预留须落在已授权的实验上限内，后端最大输出也须在每次输出上限内。当前场景登记使用 USD。首场 `following_scenes` 的节点与过程来自本地已采用卷计划；提交前还须在已授权作品中核实。两名读者以不同化名登记，对文本独立阅读、评分及分歧的证据在正文产生后收集。

退出码 0 表示文件与本机可执行源码、模型环境的离线检查匹配；退出码 2 表示需要补齐或修正。脚本阻止 Python socket 操作，仅读取文件和 Git 元数据，没有启动服务、登记角色任务或调用模型。输出区分源码、路由、配置材料、阅读人安排及消费账实。正式登记时由 Kernel 校验当前 HEAD、主管授权、真实预算和已采用计划。角色工具所需的原生 Host 隔离仍须单独实测。

**跨全卷 Token 预算**。本次核对的 NF `55d8979b` 在同一资金账户上硬限总金额，在各 Task 上分别硬限 Token。单份实验授权里的 `total_tokens` 仅供离线对照，不能强制多个任务共享全卷 Token 配额。准备真实创作时，先落实按整次实验限制 Token 的可验证部署控制（如隔离供应商项目的硬配额），保留该控制的测试证据并核对预算余额。UNKNOWN 继续保留原 attempt 的款项及 Token 预留。

本机在 #6 的 `55d8979b5361a4837701d762feacd83036096b11` 上用空模板执行的[实际回执](P6_LOCAL_PREFLIGHT.json)为 `BLOCKED_ENVIRONMENT`。另用合成、不可路由的 `.invalid` 端点仅检查本地正负路径。完整合成封套通过，金额、Token、环境模型、单次输出不符，重复演员、错误 WorldRef、不同 Git 或 Skill 树均被拒绝；以上五次运行均未联网。可在已安装依赖且源码干净的工作树重现：

```sh
cd "$NF_CHECKOUT"
uv run --no-sync python "$P6_TOOLKIT/scripts/P6_preflight_smoke.py" --nf-checkout "$NF_CHECKOUT"
uv run --no-sync python scripts/run_supervisor.py --help
```

源码入口的 `--help` 已本机验证。实际消费授权、硬配额、私有作品及运行时前置条件齐备后，按本记录“启动现场”和本版本 `docs/P6_本地真实模型创作交接.md` 运行。验收保留首场与后继登记回执、原任务/attempt、独立文学评阅和可对账的成本。
