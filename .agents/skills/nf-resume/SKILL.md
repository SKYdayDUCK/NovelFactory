---
name: nf-resume
description: 作者说“继续未完成的任务”或要求恢复 NovelFactory 小说生产时，通过已配置的项目 MCP 入口恢复当前作品与分支。
---

调用当前 NovelFactory MCP Server 的 `nf_resume_project`，参数为空对象。以返回的 READY、WAITING、RUNNING 和已唤醒数为准。后台 Supervisor 管理已登记的就绪任务。报告回执中的任务计数。只有取得等待原因或服务状态的直接证据时才说明原因。随后结束本模型轮次，由持久事件与调度器负责下一次唤醒。若凭据过期，提示作者更新对应的受限授权。不要从环境或数据库中寻找管理员令牌，不代替角色读取剧情或认领任务。
