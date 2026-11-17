import re
from dataclasses import dataclass

from .models import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL
from .policy import MARKER

MAX_UPLOAD_BYTES = 256 * 1024
CHUNK_CHARS = 320
CHUNK_OVERLAP = 40
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
CONFIG = {
    "parser": "markdown-text-v1",
    "chunk_chars": CHUNK_CHARS,
    "chunk_overlap": CHUNK_OVERLAP,
    "provider": "bailian",
    "embedding": EMBEDDING_MODEL,
    "dimensions": EMBEDDING_DIMENSIONS,
}


class InvalidText(ValueError):
    pass


def model_passage(heading: str | None, body: str) -> str:
    return f"{heading[:120]}\n{body}" if heading else body


def decode_text(content: bytes) -> str:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidText("Expected UTF-8 text") from None
    if "\0" in text or not text.strip():
        raise InvalidText("Expected non-empty UTF-8 text")
    return text


@dataclass(frozen=True)
class Chunk:
    heading: str
    body: str
    line_start: int
    line_end: int
    start_offset: int
    end_offset: int
    section_start: int

    def passage(self) -> str:
        return model_passage(self.heading, self.body)

    def location_json(self) -> dict:
        return {"line_start": self.line_start, "line_end": self.line_end,
                "start_offset": self.start_offset, "end_offset": self.end_offset,
                "section_start": self.section_start}


def split_text(text: str, markdown: bool) -> list[Chunk]:
    chunks: list[Chunk] = []
    headings: list[tuple[int, str, int]] = []
    lines: list[tuple[int, str, int]] = []

    def flush() -> None:
        if not lines:
            return
        body = "\n".join(line for _, line, _ in lines)
        offsets = [(number, origin + column) for number, line, origin in lines for column in range(len(line) + 1)]
        heading = " / ".join(title for _, title, _ in headings)
        section_start = headings[-1][2] if headings else 0
        for start in range(0, len(body), CHUNK_CHARS - CHUNK_OVERLAP):
            end = min(start + CHUNK_CHARS, len(body))
            if body[start:end].strip():
                first_line, first_offset = offsets[start]
                last_line, last_offset = offsets[end - 1]
                chunks.append(Chunk(heading, body[start:end], first_line, last_line,
                                    first_offset, last_offset + 1, section_start))
            if end == len(body):
                break
        lines.clear()

    offset = 0
    fence = None
    for number, raw in enumerate(text.splitlines(keepends=True), 1):
        line = raw.splitlines()[0]
        origin, offset = offset, offset + len(raw)
        if number == 1 and markdown and line.startswith(MARKER):
            continue
        delimiter = re.match(r"^\s{0,3}(`{3,}|~{3,})(.*)$", line) if markdown else None
        if delimiter and fence is None:
            fence = delimiter[1]
        elif delimiter and delimiter[1].startswith(fence or "\0") and not delimiter[2].strip():
            fence = None
        match = HEADING.fullmatch(line) if markdown and fence is None else None
        if match:
            flush()
            level = len(match[1])
            headings[:] = [heading for heading in headings if heading[0] < level]
            headings.append((level, match[2], origin))
        elif line.strip():
            lines.append((number, line, origin))
        else:
            flush()
    flush()
    return chunks
