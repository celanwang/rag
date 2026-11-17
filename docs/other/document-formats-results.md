# DOCX 与文本 PDF 首版验收

## 支持范围

| 格式 | 入库内容 | 引用位置 |
| --- | --- | --- |
| Markdown / TXT | 原有 UTF-8 解析、切分和行号。Markdown 仍支持现有政策元数据。 | `location.kind=lines`，保留顶层 `line_start` / `line_end`，历史切片也无需回填。 |
| DOCX | 常见 OOXML 文档的标题、显式大纲级别、正文、超链接显示文字及简单表格。长段落与长表格行独立切分。 | `docx_paragraph`：章节路径与正文顶层段落序号（包括标题、空段落）；`docx_table_row`：章节路径、正文表序号与表内行号。序号从 1 开始。 |
| 文本 PDF | 原页逐页提取已有文本层；切片不跨页。 | `pdf_page`：从 1 开始的物理页码，跳过页面也不重编号。二进制格式的顶层行号为 `null`。 |

原件使用收到的字节保存，版本记录 SHA-256、解析配置、限额及切分参数。上传继续使用原始请求体；可用对应格式的 MIME 或 `application/octet-stream`。保留 256 KiB 原件上限。相同幂等键和输入返回原回执；改变内容返回 `409`。

查看 `GET /v1/ingestion-jobs/{job_id}` 的 `parse_report`。它记录完成、部分解析或失败，以及适用的图片页、无文本页、跳过的复杂表格和警告。部分解析且有可索引内容的版本可以发布；没有可索引内容或发生硬错误则保持之前的发布版本。已有文本任务的报告可为空。

| 结果 | 可见信号 |
| --- | --- |
| 扩展名与文件签名不符 | 上传返回 `422` 和 `format_mismatch`，不创建版本。签名看似正确、容器内容不是 Word 时则在任务中报告 `format_mismatch`。 |
| 声明的 MIME 与扩展名冲突 | 返回 `415`。 |
| 原件超出 256 KiB | 返回 `413`。 |
| 全 PDF 无可提取文字，且有图片页 | 任务终止为 `pdf_scanned`；没有识别到图片时为 `content_no_text`，例如空白 PDF。 |
| 加密、损坏、超出解析限额 | 任务分别报告 `content_encrypted`、`content_corrupt`、`content_limit`；解析进程异常退出为 `parser_failed`。 |

DOCX 首版最多接受 1,024 个归档条目、总声明展开量 8 MiB；PDF 限 100 页，有解码流和对象解析限额（包括常用过滤器的 4 MiB 解压限额）。每个二进制文件最多产生 4,096 个提取块和 256 Ki 字符，并在工作进程的子进程中执行，限时 15 秒。Linux 另有 512 MiB 进程地址空间上限。超过限额时不发布截断版本。

检索、问答和引用接口使用相同的 `location`。从引用下载时，带上 `GET /v1/documents/{id}/file?version_id={version_id}`；如果打开引用后有新版成功切换，旧链接返回 `404`，避免把新版原件当作引用来源。不传版本 ID 的旧下载链接继续返回当前发布原件。原有库授权、文档 ACL、发布切换和旧 ES 候选复核规则不变。现有 RAGAS 评测仍用原有 Markdown 文本语料与字符跨度；文档格式端到端用例另外核对二进制引用。

## 可复跑命令

在仓库根目录运行。独立 Compose 项目使用自己的端口、数据卷和配置；每次 `test` 再新建数据库和 ES 索引：

```sh
mkdir -p .rag
export RAG_DEV_PROJECT=rag-docx-pdf
export RAG_DEV_ENV_FILE="$PWD/.rag/docx-pdf.env"
test -e "$RAG_DEV_ENV_FILE" || (
  umask 077
  printf 'RAG_POSTGRES_PORT=55443\nRAG_ES_PORT=59211\n' > "$RAG_DEV_ENV_FILE"
)
uv lock --check
uv run --locked --python 3.11 python -m rag.dev up
uv run --locked --python 3.11 python -m rag.dev test
npm --prefix frontend ci
npm --prefix frontend run build
```

默认测试使用真实 PG、pgvector、ES，模型响应使用固定替身。要重复真实百炼 embedding 和 rerank 验证，可在导出 `DASHSCOPE_API_KEY` 后运行 `RAG_FORMAT_LIVE_MODELS=1 uv run --locked --python 3.11 python -m rag.dev test`；问答生成仍用可复现的固定摘录选择。专项文件为 [解析用例](../../tests/test_formats.py) 和 [链路用例](../../tests/test_format_ingestion.py)。

现有库上线时先执行 [005 迁移](../env/rag/migrations/005_ingest_parse_report.sql)，将 schema 升至 v5；本地 `rag.dev up` 自动执行。先切换入库 worker（含 Celery 入库 worker），再启用新版上传 API。无需重建已有向量、ES 索引或重排已入库的版本。

浏览器手工复跑：在上面独立环境导入样例工作区并启动 API、入库和同步 worker，使用有写权限的工作区上传 `mixed.pdf` 与 `returns.docx`。第一个任务应显示图片页 1、3 及部分解析；同步后用关键词检索“鸢尾门店退货”，打开 PDF 结果检查第 2 页。检索 `ORCHID731` 检查 DOCX 第 1 表第 2 行。通过原文引用下载，并用 `cmp` 与样本比较。

```sh
# 先提供 DASHSCOPE_API_KEY，或给以下 uv 命令传 --env-file 指定本地模型配置。
uv run --locked --python 3.11 python -m rag.dev demo
uv run --locked --python 3.11 python -m rag.dev serve
# 另一个终端；如上面的 API 使用了非默认端口，设置 RAG_API_TARGET。
npm --prefix frontend run dev
```

## 2026-11-17 实测

- 独立 PG/ES 全套 57 项通过。把样本中的同一条款依次作为同一文档的 Markdown、DOCX、PDF 版本上传。验证向量、关键词、引用与问答位置一致；用下载的原件重新定位并比对字节、MIME、SHA-256 和解析配置。实际百炼 embedding 与 rerank 的格式专项 3 项通过。
- 新版入库未完成时旧版仍可查询、引用和下载；无文本新版失败时旧版仍发布。成功切换但 ES 仍旧时不返回过时证据；文档撤权后（包括 ES 同步前）和跨租户读取均不可见。以 v4 实际写入旧引用和未消费的 TXT 任务再执行 v5 迁移，旧引用保持第 3 行，旧任务正常发布。
- Chrome 通过本地工作台上传混合 PDF 和 DOCX，并在真实服务上完成解析提示、同步、页码和表格引用、下载逐字节比对。
- 样本涵盖带表格的 DOCX、中英文文本 PDF、图片页混入文本 PDF、旧 `.doc`、复杂表格、双栏 PDF、加密、损坏、伪造容器和超限。图片页没有生成伪造文本；DOCX 合并单元格表整表跳过，其他段落仍可入库。真实旧 Word 二进制样本会拒绝，改名也不能通过。双栏样本按 PDF 内容流交错输出左右列，页码正确，阅读顺序不能保证。
- 这批合成样本未测 OCR 识别率或生产文档覆盖率。图像判定基于无可提取文字且检测到图像资源，只用来报告疑似扫描页。带隐藏文字层的扫描件按已有文字层入库；矢量轮廓、图表、复杂布局、字库映射异常的质量需另测。DOCX 页眉页脚、脚注、批注正文、文本框、跟踪修订、嵌套和合并表格、旧 `.doc` 均不在首版提取范围内。

样本来自 [formats 目录](../samples/formats/)；文本 PDF 和 DOCX 都是合成文件。
