# 阶段 A · 调度席位与重复恢复

| 检查 | 运行结果 | 可核对的证据 |
|---|---|---|
| 原 PR #5 | Draft；基点 c8d35c74；源 9dc9c6b8 | P5_STAGE_A.json 的六个 GitHub run ID |
| 基线隔离环境 | 15 项通过 | evidence/p5/handoff-a/baseline.xml |
| 旧实现延长任务租约后并发席位仍按初始时间回收 | 1 项失败 | evidence/p5/handoff-a/old-slot-expiry.xml |
| 候选调度、MCP、恢复、任务生命周期 | 40 项通过、0 跳过 | evidence/p5/handoff-a/lease-and-resume.xml |
| 既有容量 | NOT_MET | 基线 50/200 热点、500 持续读、1000 开环读未达；测试配置、统计口径和门槛沿用原仓库 |

调度席位与所属执行的任务租约在同一事务续期。续期间使用共享准入锁，和执行分配、过期回收互斥。调度器按仍在执行的原 attempt 修复旧版遗留的席位期限，已切换代际或终止的槽沿用原故障清理期限。场景保留一个作品席位、两个就绪任务，验证续租后阻止第二项，第一项确实过期后允许第二项。

原生 MCP 子进程通过 stdio 启动。一个满足前置条件的项目收到同一绑定的 100 次恢复，首次唤醒 1 项，其余 99 次唤醒 0 项；重放期间供应商请求为 0。此后两个独立合成模型执行通过重启的 Supervisor 完成。最终世界正式接受 1 个事件，供应商夹具收到了 2 次请求。

复现时，在配置了锁定依赖、PG17 和测试用 Codex CLI 的干净检出执行以下命令。原生桌面与长期容量另取相同 SHA 的独立工作流。

~~~bash
python scripts/with_postgres.py --migrate -- python -m pytest -q tests/integration/test_p5_scheduler_admission.py tests/integration/test_p5_mcp.py tests/integration/test_p5_process_resume.py tests/reliability/test_workflow.py tests/reliability/test_tasks.py --junitxml=stage-a.xml
~~~
