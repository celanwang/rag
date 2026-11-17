# SQL 环境

## PoC 数据库

`rag-poc` 的 PostgreSQL 容器将 [poc/schema.sql](../../poc/schema.sql) 挂载到 `/docker-entrypoint-initdb.d/`。空数据卷首次启动时执行该脚本，在 `rag` 数据库中启用 `vector` 扩展、创建 `documents` 和 `chunks`，并为两表配置行级安全（RLS）。PoC API 使用受限角色 `rag_app`，宿主机连接端口为 `55432`。API 可通过 `RAG_POSTGRES_DSN` 指定连接。 [Compose 配置](../../compose.yaml)

PostgreSQL 启动后，从仓库根目录检查扩展、业务表和 RLS 状态。

```sh
docker compose exec -T postgres psql -U postgres -d rag \
  -c "SELECT extversion FROM pg_extension WHERE extname = 'vector';" \
  -c "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;"
```

结果应包含 `vector` 的版本，以及 `chunks` 和 `documents` 两行，两表的 `rowsecurity` 均为 `t`。租户隔离和连接复用由 `poc.verify` 验证。 [验证命令](../../poc/README.md#local-retrieval-poc)

## 首期数据库规划

首期服务使用独立的空数据库，通过初始化 SQL 创建业务表、外键和 RLS。业务验收使用上传接口将样本文档导入首期环境。 [首期八表设计](../plan/schema.md)
