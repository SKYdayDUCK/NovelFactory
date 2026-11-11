# W5 已部署数据的前向升级与物理重启

本次检出 `455cf92cc6fe5e1d0e565d627a4d5651a56bc8d4`，Git 树 `d6cf8513c8df97902eb08fe1666492097d3d025a`。四个新建 PostgreSQL 17.11 数据库分别安装历史 W4c、W5、W5a、W5b，再用同期应用写入真实业务记录，连续两次升级到现行 W5。四个用例均通过，零跳过，耗时 22.982 秒。

每份回执包含旧列数据哈希、继承的审核权限、触发器模式和新旧目录对照。两条未具备正式归属的 W5 唤醒请求均撤销，执行次数为零，W5b 已授权的原请求只执行一次。各库均启动多个实际 worker 进程核对请求处置，NF 对端采用离线夹具。随后一起物理停止并重启数据库，再次验证四库逐表摘要、审核权限和唤醒账本。外部商业模型调用为零。

[原始 JUnit、各版本数据、物理重启及脚本](historical-upgrades.zip) · [数据与工件校验和](summary.json) · [修复前的旧库负控](https://github.com/SKYdayDUCK/NovelFactory/tree/741e465ef9374541f610ff209af093f653bf4c97/reports/integration/evidence/W5/historical-upgrade-rehearsal)
