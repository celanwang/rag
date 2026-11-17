from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field


@dataclass(frozen=True)
class TextLocation:
    line_start: int
    line_end: int
    kind: Literal["lines"] = "lines"


@dataclass(frozen=True)
class PdfLocation:
    page: int
    kind: Literal["pdf_page"] = "pdf_page"


@dataclass(frozen=True)
class DocxParagraph:
    section: tuple[str, ...]
    paragraph: int
    kind: Literal["docx_paragraph"] = "docx_paragraph"


@dataclass(frozen=True)
class DocxTableRow:
    section: tuple[str, ...]
    table: int
    row: int
    kind: Literal["docx_table_row"] = "docx_table_row"


BinaryLocation = Annotated[PdfLocation | DocxParagraph | DocxTableRow, Field(discriminator="kind")]
Location = Annotated[TextLocation | PdfLocation | DocxParagraph | DocxTableRow, Field(discriminator="kind")]
