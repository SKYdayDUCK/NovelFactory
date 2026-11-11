# 最终数据补齐复核

独立报告审核后，根代理从实际stage4 JUnit和全部六个scale场景摘要填入最后占位：两份631/380工程通过；两500混合51/51，1000有效1181/1203而容量失败；600秒PR12000有效PASS、push11708有效和292查询准入超时NOT_MET。全部原失败保留，未把单次PASS替代重复稳定验收。

再次读取实际10个workflow runs均终态；工程/PITR通过，三个容量workflow分别失败。PR PITR attempt1外部网络失败和attempt2成功分别记录。两PR仍Draft/merged=false、base依赖未改、main仍721f155。四份报告未残留STAGE占位，最新500延迟首试、1000/600全部尝试、600无header观测均明确。

资料收尾不改变src/tests/contracts/migrations/benchmarks/.github或依赖。最终PR3须实际带入P2的local_repeats、ci_stage3、clean_install_pr2，再用完整Git树核对；父关系本身不代替文件校验。
