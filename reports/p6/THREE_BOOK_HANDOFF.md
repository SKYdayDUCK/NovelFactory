# 三书生产与双批次交接

## 在生产机启动第一批

部署到具有持久 PostgreSQL 17、对象存储和 systemd 的 Linux 主机。服务用户使用独立的、只读的源码检出和该版本的 Python 3.12 环境。源码位于 /srv/novelfactory 时可直接使用仓库中的 unit。冻结、实跑和复查都使用同一提交。私有批次目录和对象存储放在 /var/lib/novelfactory；每个批次使用新目录。已运行作品用原目录和相同数据库恢复。编辑代码或轮换已冻结的模型配置时另保留独立检出和实验记录。

服务环境文件为 /etc/novelfactory/p6-production.env。以部署端的密钥管理方式提供 NF_DEEPSEEK_API_KEY，同时设置 NF_RUNTIME_LIVE_APPROVED=1、NF_DATABASE_URL、NF_OBJECT_ROOT、NF_P6_BATCH_DIR。模型配置缺省读取 configs/deepseek-p6.example.json。使用已安装的项目与 Workshop 依赖；从源目录运行时，在服务环境中设置与安装目录一致的 PYTHONPATH。三本共用 Provider 帐户可以受相同 RPM、TPM 和并发限制，每本各有准备和创作账本。宿主机需保留数据库、对象存储、批次私有文件及 Worker 日志。将各环境文件限制为授权服务账户和管理员可读。

在实验专用数据库部署本提交的迁移。核对 nf_dispatch_limits 的 GLOBAL:* 及 ROLE:world_designer 的有效槽位；默认 GLOBAL:* 和 ROLE:* 都是 2。三书初始同时发起三个世界设计请求，需要全局、世界设计角色和实际供应商账户均允许至少三席。在专用库、三席已获授权的情况下，可在一次事务中设置全局和世界设计角色，保留其他职业和已有较高上限。共享部署先核对现有负荷与配额。

```sql
BEGIN;
UPDATE nf_dispatch_limits SET slots = GREATEST(slots, 3)
 WHERE scope = 'GLOBAL' AND subject = '*';
INSERT INTO nf_dispatch_limits(scope, subject, slots) VALUES ('ROLE','world_designer',3)
 ON CONFLICT(scope,subject) DO UPDATE SET slots=GREATEST(nf_dispatch_limits.slots, EXCLUDED.slots);
COMMIT;
```

以服务账号采用同一套环境运行。授权文件是 0600 的 JSON 对象，顶层键为 A、B、C。每个对象都要包含 preparation 和 writing，每阶段保存 total_amount、reserve_amount（十进制 USD），total_tokens、token_reservation（整数）。可选的 meter_per_dispatch=true 采用已实现的逐任务计量：为确已就绪的下一任务按已授权的单次预留补足账本，保留未决费用。要执行固定总额授权时省略这个字段。不得将预留金额作为已付费用，或在外部账务未决时擅自清除既有预留。单次 Token 预留需覆盖模型输出上限和实际输入的保守估计。不同阶段使用各自的明确授权值。

```bash
cd /srv/novelfactory
.venv/bin/python -m alembic upgrade head
.venv/bin/python scripts/p6_batch.py preflight
.venv/bin/python scripts/p6_batch.py enroll \
  --private-dir "$NF_P6_BATCH_DIR" \
  --allowances /etc/novelfactory/p6-allowances.json
```

按汉字计量的本批计划覆盖全书，现有规划中每章对应一个写作场景。新质量模式按每场 5,000 个汉字的规划目标从下限推导最低章数：A 的首轮计划须有 2–4 个互异章节目标，B 须有 6–8 个。该数值用于拆分工作，真正达标须以验收正文中的汉字计数为准。候选章数不足时，世界设计角色根据原 Attempt 的 minItems 诊断修订，独立审核通过后才会建卷。C 的正式批次目标是 100 章，后续按卷追加到目标章数。

enroll 会验证真实账号能访问两个指定模型，原样读取 examples/p6 中的三条灵感，登记独立作品后写入 enrolled.json。若中途退出，重跑相同命令。三部均登记后由部署管理员启用三个实例。

```bash
install -m 0644 adapters/systemd/novelfactory-p6-book@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now novelfactory-p6-book@{A,B,C}.service
```

关闭启动控制会话，从独立会话执行 systemctl is-active 检查三个实例，并取得退出后的新任务或回执。观察器必须在实例所在的 POSIX 主机上运行。输出指向一个尚不存在的私有目录；价格卡覆盖的日期必须包含实际有效时段。

```bash
.venv/bin/python scripts/p6_observe_batch.py \
  --private-dir "$NF_P6_BATCH_DIR" --output "$NF_P6_BATCH_DIR/observation-01" \
  --price-card reports/p6/evidence/deepseek-usd-20261113.json
```

保存启动门槛、0/120/300/600 秒快照、各范围 JSONL、最终汇总和服务日志。三方重叠秒数须来自不同作品原生 HTTP 传输区间的交集。起步时 SENT 或持有 Worker 锁只能解释调度进度。SQL 等待是采样瞬间的观测；价格卡推算、供应商发票、未知金额各自列示。覆盖不完整时保留未测字段。观察输出不含作品正文，原始材料与认证文件继续留在授权生产端。

观察器退出码 0 表示 600 秒观察完成且原生传输区间证实三书同时在途。退出码 4 表示完成了全部采样，但窗口内三书交集缺少证据；保留此目录和 summary.json（parallel_execution 为 THREE_BOOK_OVERLAP_UNPROVEN），把该窗口记为未证实。检查各书运行状态、实际配额与各次传输时段。如需再观察，使用新的输出目录和完整 600 秒窗口。退出码 3 表示启动门槛、输入或观察服务等条件未满足，可按已留存证据诊断。

## 暂停、重启与账务

查看 A、B、C 下的 worker-state.json 和 journalctl -u novelfactory-p6-book@A（按作品替换实例）。用相同版本重启原 unit。任务和代际、Attempt、来源哈希、原始回执以及精确修复契约决定恢复点。普通瞬时进程故障可由 systemd 接管；退出码 3 表示已记录的显式阻断，需要处理其原因。源码或路由变了，先恢复原冻结配置。短时间出现 IDLE 时检查数据库就绪/等待状态、准入票据、服务心跳、租约和凭据期限。跨日生产同时核对授权和账务时效。

用 scripts/p6_runtime_evidence.py 按 preparation-control.json 及后续 control.json 分别导出原生账本。只有证据证明未发送、或使用既有回执按规则恢复时才继续。对于费用未知的已发送请求，先用 p6_inspect_attempt.py、p6_provider_evidence.py 和财务回执核对原 Attempt，通过 p6_reconcile_invoice.py 记录确凿的结算；重新启动不得重复发同一付费请求。对正式编辑退回、授权不足、重复相同错误后的策略重评，记录原候选与判定后交给职权相符的内部角色。开发者仅修共用工程缺陷。新版本另行登记，保留历史拒收。

本批写作授权最长 86,400 秒。开场演绎先登记该阶段期限，进入滚动写作时只转交剩余秒数。每次开窗或跨卷规划都会核对剩余时长。未到期且授权仍有效时，可以用同一版本重启原 unit，以原任务和回执恢复。到期后即使增加预算或重启也不能续期；当前没有为进行中的滚动游标续签的受权接口。请保存原 run、最近冻结检查点、已发布卷、Attempt 和未决账单，登记受阻作品与依赖项。若需按同一 run 继续，须先补充显式重新授权与回归，保留原期限、版本及已发生费用记录。不同 run 必须单独登记。现阶段，C 的百章目标可能跨过这项期限。

## 第一批验收和第二批对照

1. 按三书独立 Work/Branch、冻结章序、场景、汉字和世界事件对账。A 为 6,000–10,000 字，B 为 30,000–60,000 字，C 的验收底线是完整 100 章，允许 100–500 章。逐卷检查事实、认知、连续性、独立编辑意见和内部 RELEASE。缺一项就按作品保留未完成原因。
2. 对每本冻结全文建立两个隔离 GPT 评阅会话。用 p6_external_review.py 的 keygen、enroll、export、sign、import、status，读取同一稿件全部分片，核对原文定位和签名。平均分为两份独立分数的算术平均，85 为最低线，95 为目标。按 p6_finalize_rolling.py 核验最终发布回执。
3. 第一批完成与独立故障复盘后，记录变更假设、原始证据、负例、回归和适用角色。只向第二批迁移已验证且获准共享的方法，保留各书正文、BELIEF 和私有证据的作用域。更新受保护来源清单，冻结新提交；沿用相同三条灵感、长度、模型路由和可比资源配置，但使用新目录与全新作品、世界、授权和账本。外部模型实际版本、价格时段、并发配置变化要披露。
4. 两批分别报告完整成书数/3、正式 RELEASE 数/3、首次有效候选数/有首次回执任务数、相同阶段与错误签名复发数、技术与内容返工的调用次数、两次独立评分和均值、真实 Token/价格估算/已结算额、冻结正文汉字/章的每小时速率、队列延迟以及实测三方传输重叠。记录未返回、UNKNOWN、被限流和未成交付的分母，防止幸存者偏差。按相同题材和长度成对比较；三对书不足以单独归因于某项知识或 Skill 变更。

## 冻结卷的编辑实质退回

1. **保留退回决定。** 质量模式在收到独立编辑的有效退回后进入 `EDITION_REVISING`。用 `python scripts/p6_frozen_edit.py status --binding "$binding"` 查看原决定的 `returned_ref`、当前任务和候选版本引用。旧的普通模式继续返回 `EDITING_RETURNED`。两条路径都保留原始 Attempt。
2. **复核候选。** 作品授权的 writer/targeted_revision 读取原稿、场景契约、编辑理由和定位到本场的正式审稿意见。新草稿引用 `prior_draft_ref`，沿用原版前驱交接。独立 reviewer/literary 按顺序阅读该场原稿和新稿并提出意见。Supervisor 恢复时校验同一卷、运行、编辑会话、世界版本和冻结父检查点。子稿与复读 Attempt 均归入作品预算和候选清单。`EDITION_HANDOFF_REQUIRED` 时，可按 `edition_ref` 定位父稿、子稿、模型读后意见及原退回决定。`EDITORIAL_NO_PROGRESS` 表示新旧正文逐场相同。
3. **正式替换仍需工程交接。** 逐场读后意见用于候选评阅，新稿仍引用父版前驱验收。正式替代需要隔离的候选验收选择，为修改及受影响后继场景重新取得事实、认知和连续性验收。独立编辑需重新阅读完整的候选有序稿件并批准精确清单。完成这些步骤并提供原子性的父版比较与切换接口后，滚动游标才可接续候选。操作员按候选清单接管，原 `ROLLING_CHECKPOINT`、`NARRATIVE_SCENE`、世界版本和已发布前缀可继续核查。[候选与逐场复读](../../src/novel_factory/application/returned_edition.py) · [冻结卷复核](../../src/novel_factory/application/frozen_editing.py) · [发布对前驱的验证](../../src/novel_factory/application/release.py)

## 中间卷的连续性复核

质量模式下，中间卷完成 RELEASE 后，Supervisor 取出每个已验收场景的首段原文（最多 600 个字符）。自动记录的含义仅为“读者已看到此段文字”。每条记录使用稳定的来源标识，状态 RESOLVED 只表示该段披露已经发生。情节线、人物动机、伏笔和客观世界变化另有各自的证据和登记入口。这组摘录不能证明该卷所有文学线索均已登记。

独立 reviewer/continuity 通过作品审核路由逐页读取完整的已发布前缀；旧页带入已批准且引用该页来源的读者记录，新页带入最近四条可见披露的压缩内容与原文位置。每页均须提交明确决定，并在清单上关联成功的 Attempt。全部通过后，系统发布这次追加的连续性快照；模型调用和成本进入本作品账本。未决判断或实质退回可查看 P6_SERIAL_PAGE 收据对应的模型输出。已有历史卷的事件选择或世界解释发生变化时，保留候选与受影响卷清单，交由现有完整的独立连续性复核入口处理。每次完整原文复核仍受原生 2 MB 已发布前缀门槛约束。
