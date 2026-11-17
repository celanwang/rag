# rag
A RAG system that finds relevant sources and uses them to answer questions.

## Features

- Find relevant sources
- Use relevant sources to answer questions
- Evaluate the quality of the answers

## Architecture

[总体设计](docs/design.md) · [数据库设计](docs/schema.md)

## Tech Stack

- Python

## Project conventions

[Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)

## 本地检索 PoC

依据 [总体设计](docs/design.md) 和 [选型验证计划](docs/research.md)，用相同题集比较 pgvector 余弦精确召回、Elasticsearch CJK BM25（含编号精确匹配）、RRF、RRF 加 BGE cross-encoder 精排。

[PoC 验证报告](docs/poc-results.md)汇总自研检索消融、RAGFlow 同题对照和隔离验收。

PoC 的 Python 模块、样本文档、题集和建表 SQL 位于 [poc/](poc/)。

### 启动与实测

需要 Docker Compose、uv 和 Python 3.11。初次运行会拉取两个服务镜像及约 390 MB 的模型和分词器文件。模型之后由本机 CPU 推理。

```sh
docker compose up -d --wait
uv sync --locked --python 3.11
uv run --locked python -m poc.models
uv run --locked uvicorn poc.api:app --host 127.0.0.1 --port 8000
```

在另一个终端执行以下命令。评测会通过 API 导入两租户共 14 份样本文档，校验金标引用，再以并发 1 对 30 道题、4 种模式各测 3 遍。用一套专用的 Compose 数据卷复跑可固定语料快照。

```sh
uv run --locked python -m poc.benchmark --repeat 3 --out .poc/results.json
uv run --locked python -m poc.verify
```

`.poc/results.json` 包含题集、语料指纹、模型版本、逐题结果、授权后候选、阶段耗时和按题型汇总。评测使用每路 8 个候选，RRF 的 `k=60`，精排最多 12 个候选，返回前 5 条。API 默认每路 50 个候选，精排最多 30 个候选，可在请求中调整。

### 导入与检索

```sh
curl -fsS http://127.0.0.1:8000/v1/knowledge-bases/demo/documents \
  -H 'Authorization: Bearer alpha-admin' -H 'Content-Type: application/json' \
  -d '{"filename":"manual.md","content":"# 设备错误\n\nDEV-120 时在管理台重新绑定设备。","readers":["reader"]}'

curl -fsS http://127.0.0.1:8000/v1/retrieve \
  -H 'Authorization: Bearer alpha-reader' -H 'Content-Type: application/json' \
  -d '{"question":"DEV-120 怎么处理？","mode":"rerank","debug":true}'
```

- 入库接受 UTF-8 Markdown 或纯文本。相同知识库、文件名和正文重复提交沿用发布版本，正文变化则切换到新版本。省略 `readers` 或传空数组允许知识库内的调用者读取。若 `keyword_ready=false`，PostgreSQL 已发布，可以重提完整请求同步 ES。
- 检索模式为 `vector`、`keyword`、`rrf`、`rerank`。`GET /v1/citations/{chunk_id}?knowledge_base_id=demo` 重新校验读取权限并返回文件名、版本和原文行号。混合模式会报告召回分支降级，证据始终经过 PostgreSQL 复核。
- 本地演示凭据绑定租户和主体。两个租户分别使用 `alpha-admin`、`beta-admin` 入库，使用 `alpha-reader`、`beta-reader` 查询。Alpha 审计文件仅向 `alpha-auditor` 开放。Compose 端口与 API 绑定回环地址。
- PoC 同步接入 Markdown/TXT，覆盖文件切分、检索消融和权限验证。真实身份提供方、异步入库、文本型 PDF/DOCX 与带引用的答案生成属于首期规划。

## RAGFlow 原生混合检索基线

用专用的 Compose 项目运行 RAGFlow 1.0.0。先按上面的步骤启动自研 PoC，并下载 BGE 模型。下面的端口和模型连接方式已在 Docker Desktop 上验证。

[RAGFlow 同题对照](docs/poc-results.md#ragflow-同题对照)记录了召回、延迟和配置差异。

```sh
mkdir -p .poc
(
  umask 077
  test -s .poc/ragflow-admin-password || openssl rand -hex 24 > .poc/ragflow-admin-password
  test -s .poc/ragflow-embedding-key || openssl rand -hex 24 > .poc/ragflow-embedding-key
)
export RAGFLOW_ADMIN_PASSWORD="$(cat .poc/ragflow-admin-password)"
docker compose -f poc/ragflow/compose.yaml up -d --wait
curl -fsS --retry 10 --retry-connrefused http://127.0.0.1:19380/api/v1/system/healthz
docker compose -f poc/ragflow/compose.yaml exec -T ragflow cat /ragflow/conf/public.pem > .poc/ragflow-public.pem
```

健康检查返回 `status=ok` 后，在一个终端启动本地嵌入服务。

```sh
RAGFLOW_EMBEDDING_KEY="$(cat .poc/ragflow-embedding-key)" \
  uv run --locked uvicorn poc.ragflow_embedding:app --host 127.0.0.1 --port 18090
```

在另一个终端创建 Alpha、Beta 两个 RAGFlow 用户和本地模型配置，然后运行对照。凭据保存在被 Git 忽略的 `.poc/`。

```sh
uv run --locked python -m poc.ragflow_setup
uv run --locked python -m poc.ragflow_baseline --repeat 3 --out .poc/ragflow-results.json
```

对照导入同一份原件，通过原文锚点对三种模式统一评分，保留每次请求、实际解析配置、源文件指纹和越权探测结果。为了对齐评测时的可见文档，RAGFlow 的 Alpha 普通读者请求附带文档白名单。脚本还用同一凭据删除和改写白名单，实测 RAGFlow 原生授权边界。
