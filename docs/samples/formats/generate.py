"""Rebuild the small, synthetic formats corpus with ReportLab 4.4.9."""
from io import BytesIO
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from PIL import Image, ImageDraw
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

HERE = Path(__file__).resolve().parent
CLAUSE = "鸢尾门店退货须在签收后十四天内申请。 Iris returns must be requested within 14 days."
WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def paragraph(text, style=""):
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{props}<w:r><w:t>{escape(text)}</w:t></w:r></w:p>"


def row(*cells, merged=False):
    props = '<w:tcPr><w:gridSpan w:val="2"/></w:tcPr>' if merged else ""
    return "<w:tr>" + "".join(f"<w:tc>{props}{paragraph(cell)}</w:tc>" for cell in cells) + "</w:tr>"


def word_file(path, body):
    parts = {
        "[Content_Types].xml": (
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>'
        ),
        "_rels/.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
            '2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>'
        ),
        "word/document.xml": (
            f'<w:document xmlns:w="{WORD_NS}"><w:body>{body}<w:sectPr/></w:body></w:document>'
        ),
    }
    with ZipFile(path, "w") as archive:
        for name, content in parts.items():
            archive.writestr(ZipInfo(name), content.encode("utf-8"), compress_type=ZIP_DEFLATED)


def table(*rows):
    return "<w:tbl><w:tblGrid><w:gridCol w:w=\"3000\"/><w:gridCol w:w=\"6000\"/></w:tblGrid>" + "".join(rows) + "</w:tbl>"


def pdf(path, pages):
    doc = canvas.Canvas(str(path), pagesize=(612, 792), invariant=1, pageCompression=1)
    for items in pages:
        for item in items:
            if item is None:
                scan = Image.new("L", (620, 160), "white")
                ImageDraw.Draw(scan).text((16, 30), "SCAN ONLY: replacement terms are pixels", fill=0)
                png = BytesIO()
                scan.save(png, format="PNG")
                png.seek(0)
                doc.drawImage(ImageReader(png), 72, 550, width=460, height=115)
            else:
                x, y, text = item
                doc.setFont("STSong-Light", 12)
                doc.drawString(x, y, text)
        doc.showPage()
    doc.save()


def main():
    (HERE / "returns.md").write_bytes(f"# 退货条款 / Returns\n\n{CLAUSE}\n".encode())
    word_file(HERE / "returns.docx", paragraph("门店制度 / Store rules", "Title") +
              paragraph("退货条款 / Returns", "Heading1") + paragraph(CLAUSE) +
              table(row("Rule", "Clause"), row("Exchange", "Exchange barcode ORCHID731 is required.")))
    word_file(HERE / "complex.docx", paragraph("Complex layout", "Heading1") +
              paragraph("Supported body remains.") +
              table(row("This clause spans two cells.", merged=True)))
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    text_page = [(72, 720, CLAUSE)]
    pdf(HERE / "returns.pdf", [[(72, 720, "Iris store policy")], text_page])
    pdf(HERE / "mixed.pdf", [[None], text_page, [None]])
    pdf(HERE / "columns.pdf", [[(72, 720, "Left column line 1"), (320, 720, "Right column line 1"),
                                (72, 690, "Left column line 2"), (320, 690, "Right column line 2")]])


if __name__ == "__main__":
    main()
