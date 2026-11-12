# 多作品准入与限额

为每个作品启动使用自身访问文件的 supervisor。数据库中的项目任务、知识断面、费用预留和模型 Attempt 保持独立。所有连接到同一 NF 数据库的 worker 争用同一个短调度事务。旧版 `nf_dispatch_limits` 的全局、作品和角色槽位继续生效，默认各为 2。每部作品的进程可以使用不同数量的 worker。

安装管理员可在 WSL2 中通过以下命令管理 P5.8 额外的执行容量，参数须替换为本地标识。provider / account 来自模型目录中不可变的底层服务分组和凭据引用。多个 profile 共用同一实际账号时，使用同一个凭据环境变量名。以下命令在拥有 NF 数据库凭据的受信 shell 中运行。普通角色无此入口。

```bash
python scripts/runtime_limits.py limit work "$WORK_A" --slots 1
python scripts/runtime_limits.py limit work "$WORK_B" --slots 1
python scripts/runtime_limits.py limit organization "$INSTALLATION/$ORG" --slots 2
python scripts/runtime_limits.py limit account "$ACCOUNT_KEY" --slots 2 --rpm 5 --tpm 300000
python scripts/runtime_limits.py weight "$WORK_A" 2
python scripts/runtime_limits.py org-weight "$INSTALLATION/$ORG" 1
```

每个项目应先采用一份工作坊策略，使显式 profile 调用也能绑定正确的组织。存在组织限额而作品尚未绑定时，调度会等候。限额管理员可通过 `python scripts/runtime_policy.py --access-file "$ACCESS" preview --role writer --kind scene_draft` 查看命中规则；实际 provider、account 分组可从 `agent_runtime.providers.read_profiles` 在本机读取其 `provider_key` 和 `account_key`，无需读取凭据文件或进行请求。

系统先筛选具备预算授权与执行资格的就绪工作，按组织、作品的已服务份额与权重排队。同一等待队列超过 30 秒时，首先服务等待最久且具备配额的申请。每次成功发放后更新持久化的公平份额。角色槽位、模型平台和账号槽位的上限同时生效。发放额度前从 NF 账本预留费用与 Token，在发送权即将提交时重新检查授权、并发槽和最近 60 秒的请求量、预留 Token 量。发送前竞争失败的调用持有可核对的未发送记录；完成释放后由原调度器以新代际重试。达到并发或滑窗限额的队列会等待槽释放通知或下一次持久扫描。

`nf_runtime_permits` 记录执行租约与发送窗口。其 token 列是限流时的保守预留，最终用量与费用通过现有 NF 账本查询。Worker 崩溃时保守等待租约和传输的终止期限，已发送而未知的 Attempt 保留对账责任。
