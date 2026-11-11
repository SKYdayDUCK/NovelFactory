# Scion 架构研究与 NovelFactory 适配分析

第一阶段的正式研究正文统一维护在 [Scion 技术研究与 NovelFactory 适配](Scion_技术研究与NovelFactory适配.md)，避免两份同主题报告发生分歧。

该报告包含官方资料核验、十二项设计映射、Piper/Mononoke/Bonsai/MVCC/CQRS/增量视图的证据边界、基线源码调用链、十个架构问题逐项回答、原型候选与不变量、派生索引/缓存水位，以及一手来源与阅读范围。

性能诊断见 [NovelFactory 多 Agent 并发性能诊断](NovelFactory_多Agent并发性能诊断.md)。实际交付见[增量技术设计](增量世界版本与状态投影_技术设计.md)、[新旧内核等价性验证](新旧世界内核_等价性验证.md)、[并发性能对照](Scion思路_并发优化实验.md)和[集成与审核](并发优化_集成与PR审核.md)。
