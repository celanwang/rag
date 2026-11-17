# 开源选型与试点验证

## 首批选型

| 领域 | 选择 | 依据 |
| --- | --- | --- |
| 权限与检索服务 | 自研 API，PostgreSQL 保存授权 | RAGFlow 私有数据集通过跨租户探测，同租户 API Key 可绕过请求白名单访问审计文档。自研 PoC 的服务端 ACL 能过滤陈旧 ES 索引中的越权证据。 [授权验证](poc-results.md#隔离与故障验证) |
| 向量和关键词 | pgvector 精确搜索、ES BM25、短语和编号匹配、RRF 融合、可选精排 | 30 道合成题上，自研 RRF 的 Recall@5 为 98.3%，加精排为 100%。p95 分别为 12.5 ms 和 126.4 ms。 [同题对照](poc-results.md#ragflow-同题对照) |
| 入库执行 | PostgreSQL 任务表、入库与 ES 同步 Worker | 待发布版本和入库任务同事务提交。发布新版本时，同事务创建 ES 同步任务。根据队列积压和数据库负载评估是否引入 Celery 与消息代理。 [首期设计](design.md) |
| 解析 | 首批支持 Markdown/TXT，Docling 用于验证文本型 PDF/DOCX | 抽检解析后的阅读顺序和原文定位，并记录失败率。 [Docling 格式支持](https://docling-project.github.io/docling/usage/supported_formats/) |

## 参考项目

| 项目 | 可参考的部分 |
| --- | --- |
| `rag-center` | 租户、知识库、文档和切片的层级关系。向量存于切片，索引版本与块关系分别服务于整库切换和文档图谱。 |
| RAGFlow | 原生混合检索作为效果基线，解析能力可按文件格式单独比较。 [1.0 源码发布](https://github.com/infiniflow/ragflow/releases/tag/v1.0.0) |
| Dify、Onyx、MaxKB | 分别参考外部知识库接入、权限同步与连接器、中文管理体验。 [Dify](https://docs.dify.ai/en/cloud/use-dify/knowledge/external-knowledge) · [Onyx](https://docs.onyx.app/overview/core_features/connectors) · [MaxKB](https://maxkb.cn/docs/v2/index.html) |
| Haystack、MinerU、RAGAS、Langfuse | 分别参考检索编排、复杂解析、离线评估和链路观测。 [Haystack](https://docs.haystack.deepset.ai/docs/documentjoiner) · [MinerU](https://github.com/opendatalab/MinerU) · [RAGAS](https://docs.ragas.io/en/stable/howtos/applications/evaluate-and-improve-rag/) · [Langfuse](https://langfuse.com/docs) |

## 试点验证计划

- 真实或脱敏题集覆盖编号、语义改写、跨文档和权限差异，金标定位到原文。PDF/DOCX 样本记录阅读顺序、引用定位和解析失败率。
- 在目标并发和权限范围内测试单路、双路与精排，固定题集、超时和候选预算。每种模式报告 Recall@K、p95 和 PG 发布到 ES 可搜的时间差。评估近似向量索引时，使用同一题集与精确搜索对照。
- 已知越权用例须零泄露。撤权、删除、乱序发布和索引恢复按首批验收标准验证。带引用问答接入后，单独评估答案正确性、引用覆盖和证据不足时的表现。 [首批验收标准](design.md#首批验收标准)
