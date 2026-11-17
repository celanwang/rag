# 项目文档

## Plan

- [首期设计](plan/design.md) · 交付范围、接口和验收标准
- [数据库设计](plan/schema.md) · 首期八张业务表及权限、版本和任务约束
- [开源选型与试点验证](plan/research.md) · 选型依据和业务试点计划
- [PoC 验证报告](poc-results.md) · 自研检索消融、RAGFlow 对照和隔离实测

## Env

- RAG 业务服务 · [Compose 配置](env/rag/compose.yaml)、[PostgreSQL 初始化 SQL](env/rag/schema.sql)、[运行与验收](../rag/README.md)
- 自研 PoC · [Compose 配置](env/poc/compose.yaml)、[PostgreSQL 初始化 SQL](env/poc/schema.sql)
- RAGFlow 基线 · [Compose 配置](env/ragflow/compose.yaml)

启动和检查命令见 [PoC 运行手册](../poc/README.md)。
