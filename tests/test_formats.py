import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf import PdfReader, PdfWriter

from rag.format_parser import DOCX, PDF, DOCX_EXPANDED_BYTES, PDF_PAGES, ParseFailure, parse_binary
from rag.locations import DocxParagraph, DocxTableRow, PdfLocation
from rag.parse import prepare_binary, version_config
from rag.text import CHUNK_CHARS

FORMATS = Path(__file__).resolve().parents[1] / "docs" / "samples" / "formats"
CLAUSE = "鸢尾门店退货须在签收后十四天内申请。"
TABLE_CLAUSE = "Exchange barcode ORCHID731 is required."


def sample(filename):
    return (FORMATS / filename).read_bytes()


def pdf_variant(*, scanned=False, encrypted=False, pages=0):
    writer = PdfWriter()
    if pages:
        for _ in range(pages):
            writer.add_blank_page(width=72, height=72)
    else:
        source = PdfReader(BytesIO(sample("mixed.pdf")))
        writer.add_page(source.pages[0 if scanned else 1])
    if encrypted:
        writer.encrypt("secret", algorithm="RC4-128")
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def docx_variant(*, content=None, extra=None):
    stream = BytesIO()
    with ZipFile(BytesIO(sample("returns.docx"))) as original, ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        for name in original.namelist():
            archive.writestr(name, content if content is not None and name == "word/document.xml" else original.read(name))
        if extra:
            archive.writestr("word/media/large.dat", extra)
    return stream.getvalue()


def generic_zip():
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("not-word.txt", "plain ZIP")
    return stream.getvalue()


class FormatParserTest(unittest.TestCase):
    def test_word_order_headings_and_rows(self):
        parsed = parse_binary(DOCX, sample("returns.docx"))
        title, heading, paragraph, table_heading, clause = parsed.blocks
        section = ("门店制度 / Store rules", "退货条款 / Returns")
        self.assertEqual((title.location.paragraph, heading.location.section), (1, section))
        self.assertIn(CLAUSE, paragraph.text)
        self.assertEqual(paragraph.location, DocxParagraph(section, 3))
        self.assertEqual((table_heading.location, clause.location), (DocxTableRow(section, 1, 1), DocxTableRow(section, 1, 2)))
        self.assertEqual(paragraph.section_start, clause.section_start)
        self.assertEqual(clause.text, "Exchange\t" + TABLE_CLAUSE)
        self.assertEqual(parsed.report.status, "complete")

    def test_pdf_preserves_page_numbers_across_skips(self):
        for filename, pages, skipped in (("returns.pdf", [1, 2], []), ("mixed.pdf", [2], [1, 3])):
            with self.subTest(filename=filename):
                parsed = parse_binary(PDF, sample(filename))
                self.assertEqual([block.location for block in parsed.blocks], [PdfLocation(page) for page in pages])
                self.assertIn(CLAUSE, parsed.blocks[-1].text)
                self.assertEqual(parsed.report.scan_pages, skipped)
                self.assertEqual(parsed.report.status, "partial" if skipped else "complete")

    def test_unsupported_docx_table_is_partial(self):
        parsed = parse_binary(DOCX, sample("complex.docx"))
        self.assertEqual((parsed.report.status, parsed.report.skipped_tables), ("partial", [1]))
        self.assertEqual([b.text for b in parsed.blocks], ["Complex layout", "Supported body remains."])

    def test_docx_chunk_fragments_keep_paragraph_position(self):
        phrase = "Long paragraph with a reference. " * 25
        content = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + phrase.encode() + b"</w:t></w:r></w:p></w:body></w:document>"
        chunks, _ = prepare_binary(DOCX, docx_variant(content=content), version_config(DOCX))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(chunk.source == DocxParagraph((), 1) and len(chunk.body) <= CHUNK_CHARS for chunk in chunks))
        self.assertEqual(chunks[0].body[-40:], chunks[1].body[:40])

    def test_rejections_distinguish_scanned_empty_encrypted_corrupt_spoofed_and_limits(self):
        for mime, body, expected in (
            (PDF, pdf_variant(scanned=True), "pdf_scanned"),
            (PDF, pdf_variant(pages=1), "content_no_text"),
            (PDF, pdf_variant(encrypted=True), "content_encrypted"),
            (PDF, sample("encrypted.pdf"), "content_encrypted"),
            (PDF, b"%PDF-1.4\nnot a valid PDF\n", "content_corrupt"),
            (PDF, pdf_variant(pages=PDF_PAGES + 1), "content_limit"),
            (DOCX, generic_zip(), "format_mismatch"),
            (DOCX, sample("legacy.doc"), "format_mismatch"),
            (DOCX, sample("encrypted.docx"), "content_encrypted"),
            (DOCX, docx_variant(content=b"<broken"), "content_corrupt"),
            (DOCX, docx_variant(content=b"<!DOCTYPE root [<!ENTITY injected SYSTEM 'file:///dev/null'>]><root/>"),
             "content_corrupt"),
            (DOCX, docx_variant(extra=b"X" * (DOCX_EXPANDED_BYTES + 1)), "content_limit"),
        ):
            with self.subTest(mime=mime, expected=expected), self.assertRaises(ParseFailure) as error:
                parse_binary(mime, body)
            self.assertEqual((error.exception.code, error.exception.report.status), (expected, "failed"))
