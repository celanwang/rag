"""Extract blocks from originals. Also runs as a bounded child of the ingestion worker."""

import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from io import BytesIO
from typing import Literal
from xml.etree.ElementTree import Element, ParseError
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile
from zlib import error as ZlibError

import olefile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pydantic import TypeAdapter
from pypdf import PdfReader, apply_configuration
from pypdf.errors import DependencyError, FileNotDecryptedError, LimitReachedError, PyPdfError
from pypdf.generic import DictionaryObject

from .locations import BinaryLocation, DocxParagraph, DocxTableRow, PdfLocation

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06")
OLE_HEADER = bytes.fromhex("D0CF11E0A1B11AE1")
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CT = "{http://schemas.openxmlformats.org/package/2006/content-types}"
RELS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
MAIN_PART = "word/document.xml"
MAIN_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
MAX_TEXT_CHARS = 256 * 1024
MAX_BLOCKS = 4096
DOCX_EXPANDED_BYTES = 8 * 1024 * 1024
DOCX_ZIP_ENTRIES = 1024
PDF_PAGES = 100
PDF_STREAM_BYTES = 4 * 1024 * 1024
PDF_XREF_ENTRIES = 20_000
PDF_FORM_INVOCATIONS = 256
PARSE_TIMEOUT = 15
PARSE_MEMORY_BYTES = 512 * 1024 * 1024

# Serialized into versions.config. A changed extraction policy needs a new parser profile.
PROFILES = {
    DOCX: {
        "parser": "docx-ooxml-v1", "engine": "defusedxml-0.7.1/olefile-0.47",
        "max_zip_entries": DOCX_ZIP_ENTRIES, "max_expanded_bytes": DOCX_EXPANDED_BYTES,
    },
    PDF: {
        "parser": "pdf-pages-v1", "engine": "pypdf-7.1.0", "strict": True,
        "max_pages": PDF_PAGES, "max_stream_bytes": PDF_STREAM_BYTES,
        "max_xref_entries": PDF_XREF_ENTRIES, "max_form_invocations_per_page": PDF_FORM_INVOCATIONS,
    },
}
for profile in PROFILES.values():
    profile.update(max_text_chars=MAX_TEXT_CHARS, max_blocks=MAX_BLOCKS,
                   timeout_seconds=PARSE_TIMEOUT, linux_memory_bytes=PARSE_MEMORY_BYTES)


@dataclass(frozen=True)
class Block:
    text: str
    location: BinaryLocation
    section_start: int


@dataclass
class ParseReport:
    status: Literal["complete", "partial", "failed"] = "complete"
    warnings: list[str] = field(default_factory=list)
    page_count: int | None = None
    scan_pages: list[int] = field(default_factory=list)
    mixed_image_pages: list[int] = field(default_factory=list)
    no_text_pages: list[int] = field(default_factory=list)
    skipped_tables: list[int] = field(default_factory=list)

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)
        self.status = "partial"


@dataclass
class Parsed:
    blocks: list[Block] = field(default_factory=list)
    report: ParseReport = field(default_factory=ParseReport)
    _chars: int = field(default=0, init=False)

    def add(self, text: str, location: BinaryLocation, section_start: int) -> None:
        if "\x00" in text:
            raise ParseFailure("content_corrupt")
        if text:
            self._chars += len(text)
            if self._chars > MAX_TEXT_CHARS or len(self.blocks) >= MAX_BLOCKS:
                raise ParseFailure("content_limit")
            self.blocks.append(Block(text, location, section_start))


class ParseFailure(ValueError):
    def __init__(self, code: str, report: ParseReport | None = None):
        super().__init__(code)
        self.code = code
        self.report = report or ParseReport()
        self.report.status = "failed"


def matches_signature(mime: str, body: bytes) -> bool:
    if mime == PDF:
        return body.startswith(b"%PDF-")
    if mime == DOCX:
        return body.startswith((*ZIP_HEADERS, OLE_HEADER))
    # An isolated "%PDF-" line can be ordinary UTF-8 documentation.
    return not (body.startswith((*ZIP_HEADERS, OLE_HEADER))
                or (body.startswith(b"%PDF-") and b"%%EOF" in body[-1024:]))


def xml_part(zipped: ZipFile, name: str) -> Element:
    return ElementTree.fromstring(zipped.read(name), forbid_dtd=True)


def check_ole(body: bytes) -> None:
    with olefile.OleFileIO(BytesIO(body)) as ole:
        if ole.exists("EncryptionInfo") and ole.exists("EncryptedPackage"):
            raise ParseFailure("content_encrypted")
    raise ParseFailure("format_mismatch")


def outline(element: Element | None) -> int | None:
    if element is not None:
        level = element.get(W + "val", "")
        if level in "012345678" and len(level) == 1:
            return int(level) + 1
    return None


def heading_level(paragraph: Element, styles: dict[str, Element]) -> int | None:
    level = outline(paragraph.find(f"{W}pPr/{W}outlineLvl"))
    if level is not None:
        return level
    style = paragraph.find(f"{W}pPr/{W}pStyle")
    key = style.get(W + "val", "") if style is not None else ""
    visited = set()
    while key and key not in visited:
        visited.add(key)
        if key == "Title":
            return 0
        element = styles.get(key)
        if element is not None:
            level = outline(element.find(f"{W}pPr/{W}outlineLvl"))
            if level is not None:
                return level
        builtin = re.fullmatch(r"Heading([1-9])", key, flags=re.IGNORECASE)
        if builtin:
            return int(builtin[1])
        parent = element.find(W + "basedOn") if element is not None else None
        key = parent.get(W + "val", "") if parent is not None else ""
    return None


RUN_TEXT = {W + "t": None, W + "tab": "\t", W + "br": "\n", W + "cr": "\n", W + "noBreakHyphen": "\u2011"}
RUN_CONTAINERS = {W + key for key in ("r", "hyperlink")}
IGNORABLE = {W + key for key in ("pPr", "rPr", "bookmarkStart", "bookmarkEnd", "proofErr",
                                 "commentRangeStart", "commentRangeEnd", "commentReference",
                                 "lastRenderedPageBreak")}


def paragraph_text(paragraph: Element, report: ParseReport) -> str:
    parts = []
    nodes = [iter(paragraph)]
    while nodes:
        node = next(nodes[-1], None)
        if node is None:
            nodes.pop()
        elif node.tag in RUN_TEXT:
            parts.append((node.text or "") if node.tag == W + "t" else RUN_TEXT[node.tag])
        elif node.tag in RUN_CONTAINERS:
            nodes.append(iter(node))
        elif node.tag not in IGNORABLE:
            report.warn("docx_skipped_content")
    return "".join(parts).strip()


def simple_rows(table: Element, report: ParseReport) -> list[str] | None:
    ignored = {W + key for key in ("tblPr", "tblGrid", "trPr", "tcPr")}
    rows = []
    for row in table:
        if row.tag in ignored:
            continue
        if row.tag != W + "tr":
            return None
        cells = []
        for cell in row:
            if cell.tag in ignored:
                if cell.find(W + "gridBefore") is not None or cell.find(W + "gridAfter") is not None:
                    return None
                continue
            if cell.tag != W + "tc":
                return None
            for prop in cell.findall(f"{W}tcPr/*"):
                if prop.tag in {W + "vMerge", W + "hMerge"}:
                    return None
                if prop.tag == W + "gridSpan" and prop.get(W + "val") != "1":
                    return None
            if any(child.tag != W + "p" and child.tag not in ignored for child in cell):
                return None
            cells.append("\n".join(paragraph_text(p, report) for p in cell.findall(W + "p")))
        if not cells:
            return None
        rows.append("\t".join(cells))
    if not rows:
        return None
    return rows


def parse_docx(body: bytes) -> Parsed:
    result = Parsed()
    try:
        if body.startswith(OLE_HEADER):
            check_ole(body)
        with ZipFile(BytesIO(body)) as zipped:
            entries = zipped.infolist()
            if len(entries) > DOCX_ZIP_ENTRIES or sum(info.file_size for info in entries) > DOCX_EXPANDED_BYTES:
                raise ParseFailure("content_limit")
            if any(info.flag_bits & 1 for info in entries):
                raise ParseFailure("content_encrypted")
            if any(info.compress_type not in {ZIP_STORED, ZIP_DEFLATED} for info in entries):
                raise ParseFailure("content_corrupt")
            names = set(zipped.namelist())
            if len(names) != len(entries):
                raise ParseFailure("content_corrupt")
            if not {"[Content_Types].xml", "_rels/.rels", MAIN_PART} <= names:
                raise ParseFailure("format_mismatch")
            types = xml_part(zipped, "[Content_Types].xml")
            rels = xml_part(zipped, "_rels/.rels")
            if not any(node.get("PartName") == "/" + MAIN_PART and node.get("ContentType") == MAIN_TYPE
                       for node in types.findall(CT + "Override")) or not any(
                node.get("Type") == OFFICE_REL and node.get("Target", "").lstrip("/") == MAIN_PART
                and node.get("TargetMode") != "External" for node in rels.findall(RELS + "Relationship")
            ):
                raise ParseFailure("format_mismatch")
            root = xml_part(zipped, MAIN_PART)
            content = root.find(W + "body")
            if root.tag != W + "document" or content is None:
                raise ParseFailure("content_corrupt")
            styles = {}
            if "word/styles.xml" in names:
                styles = {node.get(W + "styleId"): node for node in
                          xml_part(zipped, "word/styles.xml").findall(W + "style")}
            if any(name.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes"))
                   for name in names):
                result.report.warn("docx_skipped_secondary_parts")
    except ParseFailure:
        raise
    except (BadZipFile, ParseError, DefusedXmlException, OSError, ValueError, RuntimeError, NotImplementedError, ZlibError):
        raise ParseFailure("content_corrupt") from None

    headings: list[tuple[int, str]] = []
    section = 0
    paragraph = 0
    table = 0
    for node in content:
        if node.tag == W + "p":
            paragraph += 1
            text = paragraph_text(node, result.report)
            if not text:
                continue
            level = heading_level(node, styles)
            if level is not None:
                headings[:] = [(rank, title) for rank, title in headings if rank < level]
                headings.append((level, text[:120]))
                section = paragraph
            result.add(text, DocxParagraph(tuple(title for _, title in headings), paragraph), section)
        elif node.tag == W + "tbl":
            table += 1
            rows = simple_rows(node, result.report)
            if rows is None:
                result.report.skipped_tables.append(table)
                result.report.warn("docx_skipped_table")
                continue
            titles = tuple(title for _, title in headings)
            for index, row in enumerate(rows, 1):
                if row.strip():
                    result.add(row, DocxTableRow(titles, table, index), section)
        elif node.tag != W + "sectPr":
            result.report.warn("docx_skipped_content")
    if not result.blocks:
        raise ParseFailure("content_no_text", result.report)
    return result


class PdfWarnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.count = 0
        self.limited = False

    def emit(self, record: logging.LogRecord) -> None:
        self.count += 1
        # pypdf logs these two limits inside Form XObjects instead of propagating them.
        cause = record.args.get("exception") if isinstance(record.args, dict) else None
        if isinstance(cause, LimitReachedError) or str(record.msg).startswith("Exceeded %(limit)d form XObject"):
            self.limited = True


def pdf_has_image(resources: DictionaryObject, seen: set[int]) -> bool:
    resources = resources.get_object()
    if not isinstance(resources, DictionaryObject) or id(resources) in seen:
        return False
    seen.add(id(resources))
    xobjects = resources.get("/XObject", DictionaryObject()).get_object()
    if not isinstance(xobjects, DictionaryObject):
        return False
    for key in xobjects:
        obj = xobjects[key]
        if isinstance(obj, DictionaryObject):
            if obj.get("/Subtype") == "/Image":
                return True
            if obj.get("/Subtype") == "/Form" and pdf_has_image(obj.get("/Resources", DictionaryObject()), seen):
                return True
    return False


def parse_pdf(body: bytes) -> Parsed:
    result = Parsed()
    logger = logging.getLogger("pypdf")
    warnings = PdfWarnings()
    logger.addHandler(warnings)
    try:
        with apply_configuration(
            maximum_declared_stream_length=PDF_STREAM_BYTES,
            array_based_stream_maximum_output_length=PDF_STREAM_BYTES,
            zlib_maximum_output_length=PDF_STREAM_BYTES, lzw_maximum_output_length=PDF_STREAM_BYTES,
            run_length_maximum_output_length=PDF_STREAM_BYTES, brotli_maximum_output_length=PDF_STREAM_BYTES,
            stream_decoding_work_maximum_length=PDF_STREAM_BYTES * 2,
            page_tree_maximum_entries=PDF_PAGES * 4, xform_maximum_invocations_per_extraction=PDF_FORM_INVOCATIONS,
            jbig2dec_binary=None, disable_legacy_handling=True,
        ):
            try:
                reader = PdfReader(BytesIO(body), strict=True, xref_stream_maximum_entries=PDF_XREF_ENTRIES)
            except DependencyError:
                # The constructor may try an empty AES password before exposing is_encrypted.
                raise ParseFailure("content_encrypted") from None
            if reader.is_encrypted:
                raise ParseFailure("content_encrypted")
            result.report.page_count = len(reader.pages)
            if result.report.page_count > PDF_PAGES:
                raise ParseFailure("content_limit", result.report)
            for page_no, page in enumerate(reader.pages, 1):
                inline_image = False

                def visit(operator, _operands, _cm, _tm):
                    nonlocal inline_image
                    if operator == b"INLINE IMAGE":
                        inline_image = True

                text = page.extract_text(visitor_operand_before=visit).strip()
                if warnings.limited:
                    raise ParseFailure("content_limit", result.report)
                has_image = inline_image or pdf_has_image(page.get("/Resources", DictionaryObject()), set())
                if text:
                    result.add(text, PdfLocation(page_no), page_no)
                    if has_image:
                        result.report.mixed_image_pages.append(page_no)
                        result.report.warn("pdf_mixed_page_images_not_extracted")
                elif has_image:
                    result.report.scan_pages.append(page_no)
                    result.report.warn("pdf_scan_pages_skipped")
                else:
                    result.report.no_text_pages.append(page_no)
                    result.report.warn("pdf_no_text_pages_skipped")
    except ParseFailure:
        raise
    except FileNotDecryptedError:
        raise ParseFailure("content_encrypted", result.report) from None
    except LimitReachedError:
        raise ParseFailure("content_limit", result.report) from None
    except (PyPdfError, OSError, ValueError, IndexError, KeyError, TypeError, AttributeError,
            NotImplementedError, ZlibError, OverflowError):
        if warnings.limited:
            raise ParseFailure("content_limit", result.report) from None
        raise ParseFailure("content_corrupt", result.report) from None
    finally:
        logger.removeHandler(warnings)
    if warnings.count:
        result.report.warn("pdf_parser_warning")
    if not result.blocks:
        code = "pdf_scanned" if result.report.scan_pages else "content_no_text"
        raise ParseFailure(code, result.report)
    return result


def parse_binary(mime: str, body: bytes) -> Parsed:
    if mime not in PROFILES or not matches_signature(mime, body):
        raise ParseFailure("format_mismatch")
    try:
        return parse_pdf(body) if mime == PDF else parse_docx(body)
    except (MemoryError, RecursionError):
        raise ParseFailure("content_limit") from None


def main() -> None:
    if sys.platform.startswith("linux"):
        import resource

        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        budget = min(PARSE_MEMORY_BYTES, hard) if hard != resource.RLIM_INFINITY else PARSE_MEMORY_BYTES
        resource.setrlimit(resource.RLIMIT_AS, (budget, hard))
    try:
        parsed = parse_binary(sys.argv[1], sys.stdin.buffer.read())
    except ParseFailure as error:
        output = {"error": error.code, "report": asdict(error.report)}
    else:
        output = {"blocks": [asdict(block) for block in parsed.blocks], "report": asdict(parsed.report)}
    json.dump(output, sys.stdout, ensure_ascii=True)


BLOCKS = TypeAdapter(list[Block])
REPORT = TypeAdapter(ParseReport)


if __name__ == "__main__":
    main()
