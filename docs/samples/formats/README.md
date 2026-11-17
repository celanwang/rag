# 格式测试样本

`returns.md`、`returns.docx` 和 `returns.pdf` 都含同一条鸢尾门店退货条款。DOCX 另含带 `ORCHID731` 的简单表格，`mixed.pdf` 的第 1、3 页只有图片，`columns.pdf` 的内容流交错绘制左右栏，`complex.docx` 有合并单元格，`legacy.doc` 是实际的旧 Word 容器，`encrypted.docx` 是加密 OOXML 容器，`encrypted.pdf` 是 AES-256 加密的文本 PDF。所有内容为合成测试数据。

普通样本可在仓库根目录重建：

```sh
uv run --no-project --with reportlab==4.4.9 python docs/samples/formats/generate.py
```

旧 Word 样本用 macOS 的 `textutil -convert doc -output docs/samples/formats/legacy.doc docs/samples/formats/returns.md` 生成。加密样本用 `msoffcrypto-tool==5.4.2` 的 `OfficeFile(...).encrypt("synthetic-test-password", output)` 生成，盐值随机。AES PDF 用带 crypto extra 的 pypdf 7.1.0 复制 `returns.pdf` 的第 2 页并以相同测试密码加密。上述样本已纳入仓库，可直接运行测试，无需生成工具。测试还会动态生成扫描、加密、空白及超页数 PDF，并构造损坏及超限输入。

`policy/` 保存跨格式评测的售后政策和两份换班制度。`policy/mixed-policy.pdf` 来自 `mixed.pdf` 前两页的合成，第 1 页同时含图片和可提取退货条款。评测也复用 `returns.docx` 和 `returns.pdf`，以记录的 SHA-256 校验文件字节。 [评测入口](../../../rag/README.md#evaluate-a-policy-across-document-formats)
