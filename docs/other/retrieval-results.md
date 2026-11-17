# 业务检索验收

2026 年 11 月 17 日在 Apple M4 Max、macOS 15.5 上实测。业务环境运行 PostgreSQL 16.15、pgvector 0.8.7 和 Elasticsearch 8.19.16，向量与精排调用阿里云百炼。 [运行手册](../../rag/README.md)

## 电商端到端

- 两个租户通过上传 API 导入四份 Markdown/TXT 样例，完成向量入库和 ES 同步。四种模式均返回本租户证据，引用定位到原文行号，下载内容与上传字节一致。 [电商样例](../samples/ecommerce)
- 在 ES 同步前撤销文档读取权限，候选仍能从 ES 召回，PostgreSQL 复核会拦截。跨租户和已失效的引用或原件返回 `404`。撤销知识库读权限后，检索返回 `403`。
- 实际停止测试 ES 后，`keyword` 返回 `503`。`rrf` 和 `rerank` 标记 `degraded=["keyword"]`，继续从 pgvector 返回经 PostgreSQL 复核的证据。
- 23 项业务集成测试通过，覆盖伪造 ES 候选、精排途中撤权、S3 读取途中撤权、陈旧版本和删除，以及 PostgreSQL 复核不可用时失败。原 PoC 隔离回归也通过。 [检索集成测试](../../tests/test_retrieval.py)

## 原 30 题实测

使用原 14 份文档、30 道题和 35 个证据金标。重新通过业务上传 API 导入，评测 ES 索引仅含这批语料的 60 个切片。两条召回路径各取最多 8 个候选，RRF `k=60`，精排最多取 12 个候选，返回前 5 条。预热后以并发 1 交错跑三轮，每种模式 90 次请求，合计 360 次。各模式的逐题排序在三轮中一致。 [原题集](../../poc/questions.json)

| 模式 | Recall@1 | Recall@3 | Recall@5 | HTTP p50 | HTTP p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| pgvector | 91.7% | 100% | 100% | 168.5 ms | 512.0 ms |
| ES 关键词 | 68.3% | 83.3% | 88.3% | 19.8 ms | 32.9 ms |
| pgvector + ES + RRF | 81.7% | 98.3% | 98.3% | 163.9 ms | 435.6 ms |
| RRF + 百炼精排 | 91.7% | 100% | 100% | 346.6 ms | 538.9 ms |

B15 有两个证据锚点，关键词和 RRF 的前五条各覆盖一个，向量和精排各覆盖两个。向量模式的查询向量化 p50 为 144.1 ms，数据库向量召回 p50 为 6.6 ms。`rerank` 模式的百炼精排阶段 p50 为 179.1 ms。HTTP 延迟包含网络请求和两次证据复核，历史 PoC 的本地 ONNX 模型有不同的运行条件。 [历史 PoC 报告](poc-results.md)

以 50 ms 间隔轮询任务接口，观察到 PG 发布至 ES 可搜的间隔 p50 为 534.2 ms，最大 970.3 ms。该值包含同步 Worker 轮询和 HTTP 状态采样时间。

在固定 `rag-dev` 上复跑时，执行 `uv run --locked --env-file .env python -m rag.dev benchmark --repeat 3`。新一轮的环境清单和逐题结果保存在 `.rag/dev/<run>/`。本报告的原始记录保存在本地 `.rag/benchmark.json`。 [复跑配置](../../rag/README.md#persistent-rag-dev)

## 首期收尾验收

- `uv run --locked python -m rag.dev test` 通过 27 项集成测试。新增覆盖留存预览、租户隔离、活跃任务保护、ES 同步前保留、并发文档锁和幂等回执。日志测试覆盖库授权与撤权、文档权限变更与删除，以及 HTTP、入库和索引同步之间的追踪 ID。记录位于 `.rag/dev/test-20261117-080645-f6607d/test.log`。
- 电商端到端验收使用真实百炼和 ES。将样例时间调至留存窗口外后，清理一个过期历史版本和一个已同步删除的版本，共删除两份原件和四个切片。原上传任务仍可查询，新版在删除前可检索和下载，另一租户的原件仍可下载。核对了三次上传、一次文档读权限变更和一次删除的五条请求链，以及 CLI 撤权审计。结果位于 `.rag/dev/closeout-20261117-080911-e08589/acceptance.json`，请求和 Worker 日志位于相同目录的 `services.log`。 [留存与审计操作](../../rag/README.md#apply-version-retention)
