# Docker 环境

本地环境使用两个独立的 Compose 项目。项目各自管理网络和数据卷，评测时可同时启动。

| 项目 | 服务 | 宿主机端口 |
| --- | --- | --- |
| `rag-poc` | PostgreSQL + pgvector、Elasticsearch | PostgreSQL `55432`，ES `59200` |
| `ragflow-poc` | RAGFlow、MySQL、Elasticsearch、S3 兼容存储、Kvrocks、NATS | RAGFlow API `19380`，管理 API `19381` |

Compose 对宿主机发布的端口均绑定 `127.0.0.1`。PoC API 和 RAGFlow 嵌入服务在宿主机上运行，端口分别为 `8000` 和 `18090`。RAGFlow 容器通过 `host.docker.internal:18090` 访问嵌入服务。 [PoC Compose](../../compose.yaml) · [RAGFlow Compose](../../poc/ragflow/compose.yaml)

## 启动与检查

启动、本地凭据和模型配置见 [PoC 运行手册](../../poc/README.md)。从仓库根目录运行以下命令查看容器状态和依赖健康信息。

```sh
docker compose ps
docker compose logs --tail 100 postgres elasticsearch
curl -fsS http://127.0.0.1:19380/api/v1/system/healthz
```

在仓库根目录执行 `docker compose stop` 可停止 `rag-poc`。RAGFlow 项目使用其 Compose 文件执行 `stop`。数据库和索引保存在各自的命名卷中。 [PoC 数据库初始化](sql.md)

## 首期环境规划

首期业务使用独立的 Compose 项目、数据库、Elasticsearch 索引和对象存储桶。入库与索引同步各自运行 Python Worker，从 PostgreSQL 的 `jobs` 表领取任务。 [首期设计](../plan/design.md)
