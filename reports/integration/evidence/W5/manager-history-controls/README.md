# W5 恢复代际与历史可见范围预演

在 PostgreSQL 17.11、Python 3.12.14 上执行。唤醒用例运行实际 PostgreSQL 队列和 W5 worker，NF 对端回调为合成数据；没有商业供应商请求。此目录保存合并前独立复验。各次 JUnit、日志、代码对象、复跑脚本和校验值在 ZIP 中。

| 原件 | 被测代码与结果 |
|---|---|
| original / fixed | 历史基线 `bfb74f681435b12a385eb54e596c658e64629276` 同一作者请求可因过期 worker 补发一次项目恢复；连续五次中止后能领第六次。12 项中 2 失败。修复 `f63d1c3fb19956015d819c2d7b28bc3ee3c5cc2a` 为 12/12。 |
| lease-control / lease-final | 在上述修复上用真实数据库时钟等待原租约到期，项目调用尚在执行时第二个 worker 可取得新代际。13 项中 1 失败。发送前按原有期限续租的 `5605d2418cfdc85f31c4949355980993cfa6a11f` 为 13/13。第五次 RUNNING 租约崩溃后进入待核；恢复围栏推迟的 PENDING 仍可继续。 |
| history-control / history-fixed | 串行集成基线 `c8b9e3c18a6717b8840ad6be54acf398ea0a49a2` 加入 W5 的迁移汇合点。真实 w004、w004a、w004b 三套历史库均把另一个安装的付费审核条目暴露给当前审核员，另一个历史投递失去安装来源时仍返回事件。相同的 6 个用例原先 4 失败、2 通过，修复后 6/6。 |
| history-final | `dab0374d4cb00d069bcae4a420e7d4ef75264e46` 及同一迁移汇合点，加入当前负责人交接、旧任期和游标复验，7/7 通过。保留同一安装内人员自身的历史、组织级所有已结算承诺和原收据。 |

各次执行中使用的 `w005c_signed_routes.py` 来自正式迁移提交 `3fee7f4e88d8174f34cbfac78f3ccd5b98ec245f`（原件 Git blob `909dc0a8d850dd8201bbc2d56e8a4b3ad6213ce3`）。历史样本包括两套原 NF 安装的三笔真实数据库持久化结算，每笔 0.02、全组织合计 0.06；历史应用通过受控回执夹具建立这些账目。

## 在新环境复演

解压 ZIP，并提供包含 main 和历史 audit 分支的完整 NovelFactory checkout、`uv`、Python 3.12 及 PG17 服务端工具。Git bundle 的前置提交为 `6c711adf794842bdf4469ab51e89093d37761bcc` 与 `bfb74f681435b12a385eb54e596c658e64629276`。历史测试还须可读取 `71b4714`、`6f57575`、`4f7d8ca`。脚本在临时独立检出中恢复确切 Git 对象，用锁定依赖及一次性 PG17 实例运行，并核对预期用例、失败、错误和跳过计数。

```sh
export PG_BIN=/usr/lib/postgresql/17/bin
bash replay.sh history-final /path/to/NovelFactory
```

可选原件组为 `original`、`fixed`、`lease-control`、`lease-final`、`history-control`、`history-fixed`、`history-final`。归档附有脚本对 history-final 的独立复演输出。已知红测组的失败数是复演期望值。[管理者与历史原始回执 ZIP](./manager-history-controls.zip)
