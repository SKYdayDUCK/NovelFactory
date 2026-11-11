# P5-06 联合裁决与正式世界接受

可信登记将一个到期的语义互动、当前规则集、参与者及其密封槽、一个角色和最多 16 个已登记地点固定在同一 WorldRef。登记另外创建一项不调用模型的控制任务。裁决岗位使用独立的 adjudication_advisor / joint_outcome 授权、WORLD 切面和 adjudicate-actions 技能。它的授权不含世界接受、审批或管理能力。

三个角色全部完成后，AND 屏障唤醒裁决任务。服务复验每个槽的 grant、任务代际、BELIEF、故事时点、参与者和依赖指纹，向裁决轮次交付这一组密封提案、当时的规则和允许的行动范围。保留原地不动始终是合法选择；主地点和显式备用地点均可选择。未解决的问题、其他角色、未授权地点、资源或生命周期变动交由审核。

通过原有 Kernel 的合格路径，控制任务以当前正式头、审批及证书接受联合变化。选择记录的 source_kind 为 EXTERNAL_AGENT，source_ref 指向实际模型候选。正式接受、候选任务完成和交付 Outbox 在一个事务内提交。卷纲的依赖指向控制任务正式接受事件。不动时该语义互动仍按时结案，Kernel 生成修订收据和空 accepted_event_refs，角色位置保持原值。

| PostgreSQL 17.11 用例 | 观察结果 |
|---|---|
| 主地点与备用地点 | 3 次角色调用、1 次裁决调用。接受事件确实修改指定角色所在地点，卷纲随后就绪。 |
| 原地不动 | 0 个新世界事件，角色位置保持原值，卷纲收到正式结案信号。 |
| 在 Kernel.accept 返回后、交付事务提交前故障 | 已选结果与世界修改一起回滚，控制任务保持可执行。新建 Supervisor 从原候选完成交付。外部请求总数仍为 4。 |
| 伪造地点、资源变动、未决问题、缺少密封引用或角色授权在交付前被撤销 | 裁决任务进入 WAITING_REVIEW。控制任务仍为第 0 代，世界头无变化，卷纲保持 BLOCKED。 |
| Prompt 组装后、请求预留前撤销角色授权 | 授权复验阻止裁决请求并暂停该任务。数据库没有裁决调用记录，三个角色请求是全部模型流量。 |

测试上游是通过真实回环 HTTP 返回确定性文本的夹具。每次成功调用上报输入 11、输出 7 个 token，三角色加一次裁决共 44 / 28。等待阶段调用数为 0，控制任务调用数为 0，无需人工批准。真实推理质量、计费和 VS Code 扩展回执需各自验证。

从仓库根目录复现：

```bash
export PG_BIN=/usr/lib/postgresql/17/bin
uv run --no-sync python scripts/with_postgres.py --   python -m pytest tests/integration/test_p5_joint_handoff.py -q
```

应用入口是 RuntimeSupervisor.enroll 的 joint 参数。第二个及其后的获准地点放在可选的 alternative_destination_ids 数组中，顺序应与裁决任务 INPUT_MANIFEST 的 subject_ids 一致。可信场景登记入口负责创建这个配置；模型不能改写它。
