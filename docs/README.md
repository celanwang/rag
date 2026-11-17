# 项目文档

## Plan

- [首期设计](plan/design.md) · 交付范围、接口和验收标准
- [数据库设计](plan/schema.md) · 首期八张业务表及权限、版本和任务约束
- [开源选型与试点验证](plan/research.md) · 选型依据和业务试点计划

## Env

- `rag-dev` · [Compose 配置](env/rag/compose.yaml)、[初始化 SQL](env/rag/schema.sql)、[运行与验收](../rag/README.md#persistent-rag-dev)
- 自研 PoC · [Compose 配置](env/poc/compose.yaml)、[PostgreSQL 初始化 SQL](env/poc/schema.sql)
- RAGFlow 基线 · [Compose 配置](env/ragflow/compose.yaml)

自研 PoC 和 RAGFlow 基线的启动和检查命令见 [PoC 运行手册](../poc/README.md)。

## Other

- [本地工作台](../frontend/README.md) · 样例工作区、前端启动和浏览器验收
- [当前实现与数据流](other/implementation-review.md) · 代码拆解、发布和检索时序、实测发现与演进优先级
- [PoC 验证报告](other/poc-results.md) · 自研检索消融、RAGFlow 对照和隔离实测
- [业务检索验收](other/retrieval-results.md) · 电商与浏览器联调、故障降级和检索回归

## Samples

电商入库样例使用虚构的青禾商城和岚桥市集。两个租户各有一份同名退货政策，便于验证租户隔离。

- 青禾商城 · [退货规则](samples/ecommerce/alpha/returns.md)、[配送说明](samples/ecommerce/alpha/shipping.txt)
- 岚桥市集 · [退货规则](samples/ecommerce/beta/returns.md)、[优惠券说明](samples/ecommerce/beta/promotion.txt)
