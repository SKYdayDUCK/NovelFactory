# W4 通知队列停机与真实新工作进程

实际检出 `3ed83ffe779bf15e7965f2b2fc8236abaf90ff72`。在 PostgreSQL 17.11 的 `w004c` 数据库和 Redis 8.0.2 上单独运行原通知回归的本机进程版本，1/1 通过，耗时 1.394 秒。程序检查了 Redis 停机期间新增邮件及 PostgreSQL 的幂等 ACK，随后由两个新进程分别发布和消费。替换消费者收到重复通知后新增收件记录为零，PEL 为零，原始事件最终只有一行 inbox 和一条确认投递。

原 Docker 用例及其一项跳过记录保存在 [候选资格 ZIP](../independent/local-qualification.zip) 的 `workshop.xml` 中。同一 ZIP 的 `w3-redis-recovery.json` 记录双服务在清空 Redis 后的重建。本次原生补验仅执行上述单个跳过用例，将 Docker stop/start 换为真实的 redis-cli shutdown 和 redis-server 启动。归档同时保留四处夹具差异、原版测试 Git blob、本机进程运行 ID 和数据库事件回执。

[完整原始工件和可执行夹具](native-outage.zip) · [校验数据](result.json)
