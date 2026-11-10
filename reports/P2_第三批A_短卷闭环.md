# P2 第三批 A：短卷闭环与独立验收门槛

日期：2026-11-10。接管基线：`0609f680fad782c827dd3834a073f8f9bcddaf0d`。开发分支保持 `dev/p2-production-loop`。本报告只覆盖 P2-07；A–H、负载与 P3 结果在本轮综合交接报告中独立汇总。

## 实际完成

实现了通过真实应用服务和 PostgreSQL 持久数据运行的六场原创工程样卷《潮闸余灯》，冻结正文 2,204 个 Unicode code point。世界准备、卷契约、未来计划、角色行动、联合裁决、正式接受、依赖失效、局部重规划、场景细纲、固定快照并行写作、对账、审核、修改、文学记忆、整卷编辑与内部冻结均已运行。

样卷包含北仓与南埠两条独立目标线。南埠角色的旅行通过正式事件改变位置，两线在潮闸共同裁决。林澈知铜印已归自己，贺宁仍持旧信念，苏弦未获该知识；真实角色 ContextBundle 保留各自差异。合法毁印意外直接破坏未来凭证路线，服务计算出 2 个受影响节点并保留其他 4 个，旧未来任务同步报 `STALE_PLAN`。

三十袋粮只交付一次，库存由 100/0 变为 70/30。后续六场及多视角重述、改稿、记忆与发布没有第二次施加世界效果，前后世界发生数均为 7。贺宁加入灯守社的关系变化有正式事件及时间区间；合作与信任的文学解释另存源文、反证与未决项。

两个起始 writer 在独立任务中真实并行；第三场同时依赖两份已接受原文。修订任务真正读取旧稿后删除一次重复，后继实际获得修改后的精确原文，没有只传“已完成”标记。首轮审核问题、REVISE 决定、旧新 draft 以及最终 13 份多尺度报告均保留。

## 可核对入口

- [完整样卷](../evidence/p2/batch3/short-volume/run/short_volume.md)
- [运行清单](../evidence/p2/batch3/short-volume/run/run_manifest.json)
- [阶段 trace](../evidence/p2/batch3/short-volume/run/process_trace.jsonl)
- [对象索引](../evidence/p2/batch3/short-volume/run/artifact_index.json)
- [世界接受收据](../evidence/p2/batch3/short-volume/run/world_receipts.json)
- [原始模型夹具调用结果](../evidence/p2/batch3/short-volume/run/model_call_results.json)
- [作者评价模板](../evidence/p2/batch3/short-volume/run/author_feedback.md)
- [实现与复现说明](../docs/implementation/P2_端到端短卷生产.md)

## 实际验证与开发失败

早期定向 PostgreSQL 集成测试运行：首次完整通过 35.39 秒；加强前驱原文交付、审稿原文发现和修订材料交接后通过 38.38 秒。相应 XML/JSON 保存在 `evidence/p2/batch3/short-volume/attempt5.xml` 与 `final-test.xml/json`。这些旧测试和原 `cli.*` 当时未覆盖错误正文驱动的抽取缺口，不能据此确认最终对账能力。独立审核后修复及重新运行结果见下节。整个分支最终回归结果以综合交付证据为准，不把本地不同轮次重复计数。

开发失败也保留：

1. WorldDossier 的 open_questions 误用字符串而非 `{question, blocking}`，被契约拒绝后修正。
2. 带持久事实的抽取误选 SURFACE_DETAIL 或遗漏 EXISTING_EVENT_PRESENTATION 的必需 event_ref，被契约拒绝后改成有发生依据的对账。
3. 编辑 issue 决定误用 disposition，修正为既有 `action` 与 `replacement_draft_refs`。
4. `member_of` 是多值关系，不能把它的完整关系列表当单值 ENTITY 预期。保留该正式事件和最终列表语义验证；未更改领域规则或 Schema 来迁就夹具。

上述前四轮失败 XML 与 PG 会话均在同目录，未删除失败用例或降低服务的真实接受门槛。

## 独立审核发现与修复

交叉审核发现初版协调器直接把 ScenePlan 的预期值复制成正文断言，原文只用于 span；正文若将“三十袋”写成“四十袋”，仍可能通过。这是实际工程缺口，旧包移到 `pre-audit-run/`，增加 SUPERSEDED 标记并保留旧内容，旧 manifest 中的历史 PASS 不能作为本轮最终接受证据。

修复增加了独立的有限中文抽取器：从真实 TextDelivery 捕获数字、物品状态、持有人、角色认知、位置与成员关系。数量/状态由原文生成检查，交付和成员关系另与实际已接受事件效果核对。局部原文和解析值可追踪；未识别的重要句、未知状态或缺失必需事实锚点被明确阻断。它不读取黄金全文，不复制场景答案，也不声称支持通用自然语言。

新增三个真实 PostgreSQL 反例，分别让 Gateway writer 候选改为“四十袋”“铜汛印仍完好”和追加“铜汛印焕然一新”。实际存稿、抽取、对账后正文接受返回 NARRATIVE_BLOCKED，候选原文、抽取、服务阻断和世界未变证据保存在 `final-audit/short_volume_rejections/`。新增单元反例还检查事件 ID 正确但数量效果不符时仍失败，并验证普通修辞改写不会因整篇哈希不同被拒绝。

最终定向测试为 9 项通过，60.48 秒：5 项有限抽取单元、1 项完整短卷、3 项真实 PostgreSQL 错稿拒绝。测试与 CLI 证据分别为 `final-audit-test.*`、`final-audit-cli.*`，最终完整小说产物为 `run/`。本节以这些审核修复后的证据为准。`post-audit-run-v1/` 是分句边界加强前的中间成功实验，保留并标明被最终版取代；否定前缀/后缀及歧义数词的最后验证见最终单元结果。

## 三类结论必须分别理解

| 门槛 | 本批结果 | 依据 |
|---|---|---|
| 该短卷的工程闭环 | PASS（TEST_FIXTURE） | 真 PG、实际服务交接、不可变原文/批准/发布、同键重试与零重复效果 |
| 真实模型生产 | BLOCKED_ENVIRONMENT | [本轮环境检查](../evidence/p2/batch3/model_environment.json)：未配置授权 API；一次现有 Host 连通探测在模型派发前失败 |
| 文学质量与作者全文评价 | NOT_EVALUATED / PENDING_AUTHOR | 确定性文本和审稿输出不是真实模型作品评测；评价模板尚未填写 |

15 次调用均为 TEST_FIXTURE：3 次角色、7 次写作（含修订）、5 次审稿。fixture Token 与费用均为 0；真实模型调用数为 0，真实费用没有供应商账单证据，记录 null。没有把开发 Agent 撰写的固定测试文本称作正式模型生成，也没有伪造作者反馈。

## 尚未声称完成的事项

未实现通用水利或人物心理模拟，意外的具体语义选择是透明的夹具；工程只校验其合法后果。确定性事实抽取未解决自然语言语义完备性。单次实验协调入口不会自动恢复整个 Python 程序位置，恢复门槛由持久命令身份与独立故障测试限定。此处不把六场样卷扩展成长期长篇容量保证，也不把本批工程通过写为 P2 全面验收通过。
