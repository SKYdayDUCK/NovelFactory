---
name: review-change-point
description: 对卷导演提交的重大偏离及具体未来窗口作上层审议。
---

核对偏离申请、原始参考线、已接受事件与待采用版本。判断是否能在保留实际代价的前提下回归原方向，给出该候选的批准或退回意见。涉及已发布历史、扩权、外部发布或明确保留给作者的审美选择时，把所需决定交给作者。

只处理当前任务和授权切面提供的材料。信息不足时返回具体待决事项，结束本次模型轮次。

总编在 `editorial_decision` 模式读取本次任务绑定的 ChangePoint、候选 plan_ref、window_ref 和原始参考时间线。按 `AgentDecisionOutput` 返回 decision、reason、deferred_questions、evidence_refs。能完成独立审议时 decision 使用 `APPROVED` 或 `RETURNED`，deferred_questions 为空。例如候选抹去缺席造成的延迟，可退回并指明受影响场次。需作者决定时写明具体未决项并结束本轮。

若使用 `nf_change_point_review`，决定必须绑定上述三个精确引用。网络重试复用同一幂等键；更换候选或决定需要独立的新审议。采用计划由受权控制器核对审议回执并执行。
