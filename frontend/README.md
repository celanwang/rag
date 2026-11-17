# RAG 本地工作台

## 启动

在仓库根目录启动固定的 rag-dev 并导入电商样例。已有样例可直接启动服务。重新导入会生成两个带独立读写应用的样例工作区。

```sh
uv run --locked python -m rag.dev up
uv run --locked --env-file .env python -m rag.dev demo
uv run --locked --env-file .env python -m rag.dev serve
```

另开一个终端启动前端。使用 Node.js 20.x（至少 20.19）或 22.12 及以上。

```sh
npm --prefix frontend ci
npm --prefix frontend run dev
```

打开 [本地工作台](http://127.0.0.1:5173/)。首页自动读取最近成功导入的样例工作区。页面右上角切换租户。检索和知识库使用读取应用，上传、任务查询和文档变更使用同租户的写入应用。API Key 存在 Git 忽略的运行目录，由本地代理附在后端请求上。代理与 API 均监听回环地址，代理接收同源浏览器请求。

`RAG_API_TARGET` 可指定另一个本地 API 端口。使用自定义工作区时，将以下结构保存到 `.rag/web-workspaces.json`，通过 `RAG_WEB_WORKSPACES="$PWD/.rag/web-workspaces.json"` 启动前端。省略 `writer_key` 即为只读工作区。

```json
{
  "local": {
    "label": "本地知识库",
    "key": "<读取应用 API Key>",
    "writer_key": "<同租户写入应用 API Key>"
  }
}
```

## 界面

参考 rag-center 的检索、知识库和入库三条主流程。固定导航承载三个入口，检索区以问题和证据为主，右侧放召回策略与耗时。样例问题可直接填入表单。查看引用时按切片重新复核权限，展示行号、页码或 DOCX 章节位置。引用中的下载仅在引用版本仍已发布时返回对应原件。窄屏下导航折叠，文档表转为纵向列表。

- **检索工作台**支持向量、关键词、RRF 与百炼精排。查看耗时、候选授权情况和降级原因。
- **知识库**列出应用可读的已发布文档。支持文件名查找、查看版本与同步状态、下载、更新、修改读权限和删除。
- **文档入库**接受 Markdown、TXT、DOCX 及文本 PDF 原件。上传后查看解析结果、版本发布和 ES 同步，也可按任务 ID 查进度。

## 验收

```sh
npm --prefix frontend run build
uv run --locked python -m rag.dev test
```

本地联调可使用青禾商城的退货问题，切换四种策略后打开引用和下载。上传一个测试文件，等待同步，再验证知识库能查到新版。切换至岚桥市集可查看该租户自己的文档。 [后端运行手册](../rag/README.md) · [浏览器验收记录](../docs/other/retrieval-results.md#本地工作台验收)

[文档格式样本、独立 PG/ES 验收与浏览器复跑](../docs/other/document-formats-results.md)
