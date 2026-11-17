# 项目文档

## Plan

- [首期设计](plan/design.md) · 交付范围、接口和验收标准
- [数据库设计](plan/schema.md) · 首期八张业务表及权限、版本和任务约束
- [开源选型与试点验证](plan/research.md) · 选型依据和业务试点计划
- [PoC 验证报告](poc-results.md) · 自研检索消融、RAGFlow 对照和隔离实测
- [业务检索验收](retrieval-results.md) · 电商端到端验证、故障降级和 30 题实测

## Env

- RAG 业务服务 · [Compose 配置](env/rag/compose.yaml)、[权限 SQL](env/rag/schema.sql)、[入库 SQL](env/rag/ingestion.sql)、[索引同步 SQL](env/rag/sync.sql)、[检索权限 SQL](env/rag/retrieval.sql)、[运行与验收](../rag/README.md)
- 自研 PoC · [Compose 配置](env/poc/compose.yaml)、[PostgreSQL 初始化 SQL](env/poc/schema.sql)
- RAGFlow 基线 · [Compose 配置](env/ragflow/compose.yaml)

启动和检查命令见 [PoC 运行手册](../poc/README.md)。

## Samples

电商入库样例使用虚构的青禾商城和岚桥市集。两个租户各有一份同名退货政策，便于验证租户隔离。

- 青禾商城 · [退货规则](samples/ecommerce/alpha/returns.md)、[配送说明](samples/ecommerce/alpha/shipping.txt)
- 岚桥市集 · [退货规则](samples/ecommerce/beta/returns.md)、[优惠券说明](samples/ecommerce/beta/promotion.txt)
