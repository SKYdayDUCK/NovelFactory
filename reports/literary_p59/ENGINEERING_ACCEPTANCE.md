# 运行时与引文独立验收

检查对象为[修正树](https://github.com/SKYdayDUCK/NovelFactory/commit/c8781c28e34483a6c6cfb5619fab1f8cf3b45b3d)，对照 4348405。未读取独立盲稿及评语。runtime 目录通过 git 对象提取两份内容，再调用源码的 RuntimeCatalog.snapshot 和 creative_context.runtime_provenance。两端都使用同一套应用代码，该文件在两提交间无差异。

| 项目 | 实测结果 |
|---|---|
| 角色、技能目录 | 10 个 role_id、12 个 skill name，两个 catalog 字节相同。12 个 Skill 的 YAML frontmatter 全字节相同。10 个角色自报标识、标题和正文也与基线同字节。八份角色正文标为 2.0.0、目录登记 2.1.0，是两端相同的历史差异。 |
| 合法组合 | 逐角色、每个合法 task_mode 与该角色的每个合法主 Skill 的笛卡儿积有 66 项。两端实际调用 132 次，66 对完整返回对象、实际文本、默认技能、NF-CANON-1 摘要、受限模式、catalog 版本、mcp_requested_tools 与 effective_model_tools 相同；所有文本模型的实际工具列表均为空。另各自验证非法角色、非法任务、越界技能，均为 ROLE_MODE_DENIED。 |
| H1 来源 | 66 个新快照与 66 个基线快照均通过默认 provenance 实函数校验。[中间实验版](https://github.com/SKYdayDUCK/NovelFactory/commit/ecee9fc948c509996964552c3e6e5f033d0ebf1f) 同一项实测为 3/66 通过、63/66 RUNTIME_UNAVAILABLE，仅总编三种任务配 review-change-point 可用。最终树已消除回归。 |
| Workshop | 冻结清单的 nf_commit 为 df1e0cc904e3b94a360d86e3a3e00bfec3258673，NF provenance 来源为 276820316129d546b0a13118bcbe283b06507795，二者用途不同。Workshop 的两个目录摘要、10 份角色及 12 份 Skill 的 SHA-256 与最终树逐字节匹配。roster_projection.catalog_binding() 向能力协商公布旧提交与两个目录哈希，岗位映射和权限签发逻辑没有变更。结果未验证某个在运行的用户安装已经部署。 |
| 交付路径 | 最终树改变开发技能、角色导航和按任务选读的方法卡。内建快照没有自动增加新指令。引用知识须由 Host 在任务说明交付，或由已有服务按已获准的绑定素材发给相应任务。普通仓库文档没有自动加载入口。两书是受控 Host 文学实验；严谨表述应以书别、章别回执列出实际调用与实际原生边界，不宜笼统宣称新版本内建岗位完成了写作。 |
| 内部引用 | 排除两本盲评正文、独立评语，以变更的 Markdown 文件为入口核对了 92 个相对路径和 72 处链接至固定基线的文件或行，均有其目标。TASK_METHODS 的 ecee9fc948c509996964552c3e6e5f033d0ebf1f 在本地镜像不可见，经 GitHub commit API 核实为真实的远端方法版本。 |

研究三份文档有 50 个各自不同的外部链接（23＋14＋13）。17 个固定 SHA 的 GitHub 链接含 16 个文件和一个根目录，均经 GitHub contents API 精确 ref 读取。用通用网页检索器直开固定 SHA 的 /blob/ 和 raw 显示 DisabledError；这与 GitHub 对真正缺失的文件返回 HTTP 404 区别明确。七个项目中，lingfengQAQ 为 GPL-3.0，danjdewhurst 和 Picrew 为 MIT，haowjy 与 facebookresearch 为 Apache-2.0。在固定提交查询 Tomsawyerhu 及 DingyiYang 根 LICENSE 均返回 404，进一步列了两份根目录，同样没有普通 LICENSE/COPYING 一类文件。此结果只适用于核查的根目录与代码许可，不能推及第三方样文或数据。

另外 33 个页面均在网页检索中产生可读内容，含四篇 ACL 成果、作者展示网站、ConStory 项目页、14 个创作方法来源、9 条用户讨论、3 份公开测评与 1 篇专家研究。Linux DO、Reddit、Hugging Face 的原帖在本次抓取中可以阅读，不计为缺失或未核验。抽查了 14/20、22.5/28.2/20.7 个百分点、人评单位 1,000–1,500 英文词、600 本/121k token、2,480 对及约 73%/78%、SoloEnt 的 148 条评语及同题人物名 7/10 的两个样本；与各原文一致。所查链接未发现必须替换的死链。各论坛为发帖者自报，样本文档已作标记。

交付说明中应保留最终激活边界：可以报告 Markdown 工作流与方法卡交付、两篇的宿主创作和实际 NF 收据；不能报告六项注册 Skill 或十个内建岗位在最终树中已自动加载新文本，也不能拿快照一致性证明新方法提高了跨题材的读者偏好。
