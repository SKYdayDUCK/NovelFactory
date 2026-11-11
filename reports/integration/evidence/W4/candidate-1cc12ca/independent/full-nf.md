# 新候选的 NF 工程复验

公开头 `1cc12cab018213e05b70c226dcdf63974ee7dd93`、树 `cb601579cceafd8cd33d35664a31f8d9dd38c879`。独立 PostgreSQL 17.11 和真实 Linux 命名空间上的全量 `pytest tests` 有 834 项通过，0 跳过、0 失败，1 项警告。pytest 用时 697.57 秒，JUnit 汇总 697.557 秒。所有测试节点与它的唯一父提交逐项匹配。一次性 `nf_fixture` 保留了故障注入测试所需的参数 SET 权限；实际 AW/NF 服务继续采用独立的低权限账户。[该精确 SHA 的原始 XML、日志和文件哈希](local-nf-full.zip)

上一候选 `3ed83ffe` 首轮 831 通过、3 项 fixture 权限失败，定点复跑 4/4，第二轮全量 834/834。首轮的原始哈希和仅替换实际口令的公开副本继续可查。[原始记录](https://github.com/SKYdayDUCK/NovelFactory/blob/5f07125458a0249c3203c9c0d5b03ff92abdcd50/reports/integration/evidence/W4/candidate-3ed83ff/independent/local-nf-full.md)
