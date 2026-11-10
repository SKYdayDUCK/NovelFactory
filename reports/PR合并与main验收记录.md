# PR 合并与 main 验收记录

本记录随本轮检查点更新。初始 main 为 `721f155c9cc912cddfdae559afe4f2516d0967f2`。

| 对象 | 当前本轮确认状态 |
| --- | --- |
| PR #2 | 修复与重复容量验证进行中；尚未请求合并 |
| PR #3 | 恢复缺陷已复现并在独立工作树修复；待继承 P2 修复及当前 HEAD 的全部必要 CI |
| main | 尚未变更 |
| 合并条件 | 原强制容量尚未通过，不具备合并资格 |

远程提交使用 create_blob → create_tree → create_commit → 非强制 update_ref（expected SHA），随后读回 HEAD、完整树及关键文件。实际 Commit/CI、最终 merged 状态将在证据完成后写入本文件。任何未运行项保持未运行；无 `merged=true` 和新 main SHA 不声明已合并。
