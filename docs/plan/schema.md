# 数据库设计

首期使用八张业务表。CLI 和 API 可按 `code` 查找租户或知识库，例如 `alpha` 和 `ops-docs`。租户短码全局唯一，知识库短码在租户内唯一。`name` 用于展示，表间关联使用 UUID `id`。

独立环境依次初始化身份和授权表、入库表、索引同步队列权限和检索权限。 [权限 SQL](../env/rag/schema.sql) · [入库 SQL](../env/rag/ingestion.sql) · [索引同步 SQL](../env/rag/sync.sql) · [检索权限 SQL](../env/rag/retrieval.sql)

业务表位于 `rag` schema。初始化 SQL 在事务内设置 `search_path`，API、Worker 和 CLI 在连接时设置，建表和查询都可直接使用 `documents` 等表名。`SECURITY DEFINER` 函数使用独立的搜索路径，函数体显式引用业务表。

```sql
CREATE EXTENSION IF NOT EXISTS vector;

-- 保存租户的短码、名称和启用状态。
CREATE TABLE tenants (
    id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code    text NOT NULL UNIQUE CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name    text NOT NULL,
    active  boolean NOT NULL DEFAULT true
);

-- 保存注册应用及其 API 凭据摘要和有效期。
CREATE TABLE apps (
    id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant   uuid NOT NULL REFERENCES tenants (id),
    name     text NOT NULL,
    digest   bytea NOT NULL UNIQUE CHECK (octet_length(digest) = 32),  -- API 密钥的 SHA-256 摘要
    expires  timestamptz,
    active   boolean NOT NULL DEFAULT true,
    UNIQUE (tenant, id)
);

-- 保存租户下的知识库及其配置。
CREATE TABLE libraries (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL REFERENCES tenants (id),
    code      text NOT NULL CHECK (code ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    name      text NOT NULL,
    settings  jsonb NOT NULL DEFAULT '{}',
    UNIQUE (tenant, code),
    UNIQUE (tenant, id)
);

-- 记录应用对同租户知识库的读写权限。
CREATE TABLE grants (
    tenant   uuid NOT NULL,
    app      uuid NOT NULL,
    library  uuid NOT NULL,
    action   text NOT NULL CHECK (action IN ('read', 'write')),
    PRIMARY KEY (tenant, app, library, action),
    FOREIGN KEY (tenant, app) REFERENCES apps (tenant, id),
    FOREIGN KEY (tenant, library) REFERENCES libraries (tenant, id)
);

-- 保存逻辑文档的读取名单、发布版本和 ES 同步进度。
CREATE TABLE documents (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant     uuid NOT NULL,
    library    uuid NOT NULL,
    readers    uuid[],              -- NULL 继承库权限，空数组拒绝读取
    target     uuid,                -- 最近受理的版本
    published  uuid,                -- 当前可检索的版本
    revision   bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    synced     bigint CHECK (synced BETWEEN 0 AND revision),
    deleted    timestamptz,
    UNIQUE (tenant, library, id),
    FOREIGN KEY (tenant, library) REFERENCES libraries (tenant, id)
);

-- 保存每个文档版本的原始字节、指纹和处理配置。
CREATE TABLE versions (
    id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant    uuid NOT NULL,
    library   uuid NOT NULL,
    document  uuid NOT NULL,
    number    integer NOT NULL CHECK (number > 0),
    filename  text NOT NULL,
    mime      text NOT NULL,
    content   bytea NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 262144),
    checksum  bytea NOT NULL CHECK (octet_length(checksum) = 32),  -- 原件的 SHA-256
    config    jsonb NOT NULL,       -- 解析、切分和嵌入配置快照
    UNIQUE (document, number),
    UNIQUE (tenant, library, document, id),
    FOREIGN KEY (tenant, library, document)
        REFERENCES documents (tenant, library, id)
);

ALTER TABLE documents
    ADD FOREIGN KEY (tenant, library, id, target)
        REFERENCES versions (tenant, library, document, id),
    ADD FOREIGN KEY (tenant, library, id, published)
        REFERENCES versions (tenant, library, document, id);

-- 保存文档版本的检索文本、原文定位和向量。
CREATE TABLE chunks (
    id         uuid PRIMARY KEY,    -- 由版本和切片序号生成
    tenant     uuid NOT NULL,
    library    uuid NOT NULL,
    document   uuid NOT NULL,
    version    uuid NOT NULL,
    ordinal    integer NOT NULL CHECK (ordinal >= 0),
    heading    text,
    body       text NOT NULL,
    location   jsonb NOT NULL,      -- 原文行号或页码等定位
    embedding  vector(512) NOT NULL,
    UNIQUE (version, ordinal),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES versions (tenant, library, document, id)
);

-- 记录入库和 ES 同步任务的进度、租约及重试。
CREATE TABLE jobs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant       uuid NOT NULL,
    library      uuid NOT NULL,
    document     uuid NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('ingest', 'sync')),
    app          uuid,              -- 入库请求的发起应用
    version      uuid,              -- ingest 的目标版本
    revision     bigint CHECK (revision > 0),  -- sync 的变更序号
    request      text,              -- 上传幂等键
    fingerprint  bytea,             -- 上传请求摘要
    state        text NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'retry', 'done', 'failed')),
    stage        text,
    attempts     integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available    timestamptz NOT NULL DEFAULT now(),
    lease        uuid,
    deadline     timestamptz,
    error        text,
    UNIQUE (tenant, library, app, request),
    UNIQUE (document, revision),
    FOREIGN KEY (tenant, app) REFERENCES apps (tenant, id),
    FOREIGN KEY (tenant, library, document)
        REFERENCES documents (tenant, library, id),
    FOREIGN KEY (tenant, library, document, version)
        REFERENCES versions (tenant, library, document, id),
    CHECK ((request IS NULL) = (fingerprint IS NULL)),
    CHECK ((lease IS NULL) = (deadline IS NULL)),
    CHECK ((state = 'running') = (lease IS NOT NULL)),
    CHECK (
        (kind = 'ingest' AND app IS NOT NULL AND version IS NOT NULL AND revision IS NULL)
        OR (kind = 'sync' AND app IS NULL AND version IS NULL AND revision IS NOT NULL AND request IS NULL)
    )
);

CREATE INDEX ON jobs (kind, available) WHERE state IN ('queued', 'retry');
CREATE INDEX ON jobs (kind, deadline) WHERE state = 'running';
```

复合外键约束租户、库、文档和版本的归属。受保护的表启用行级安全（RLS），API 和 Worker 在业务事务内设置租户上下文。服务端写入 `readers` 时校验名单中的应用同属该租户。上传幂等键在应用和知识库范围内唯一，重试须匹配原请求摘要。只有持有有效租约且对应当前目标版本的任务可以发布。发布、撤权或删除文档时，事务同时推进 `revision` 并写入 `sync` 任务。ES 确认对应变更可搜索后更新 `synced`，检索始终按 PostgreSQL 中的当前版本和权限复核。首批每个应用一把生效凭据、每个切片一套向量。多凭据并存、用户组授权、连接器游标或整库切换需要独立管理时再引入相关表。 [入库与检索流程](design.md)
