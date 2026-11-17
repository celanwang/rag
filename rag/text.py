import re
from dataclasses import dataclass

from .models import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL

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

    def passage(self) -> str:
        return f"{self.heading[:120]}\n{self.body}" if self.heading else self.body


def split_text(text: str, markdown: bool) -> list[Chunk]:
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []
    lines: list[tuple[int, str]] = []

    def flush() -> None:
        if not lines:
            return
        body = "\n".join(line for _, line in lines)
        offsets = [number for number, line in lines for _ in range(len(line) + 1)]
        heading = " / ".join(title for _, title in headings)
        for start in range(0, len(body), CHUNK_CHARS - CHUNK_OVERLAP):
            end = min(start + CHUNK_CHARS, len(body))
            if body[start:end].strip():
                chunks.append(Chunk(heading, body[start:end], offsets[start], offsets[end - 1]))
            if end == len(body):
                break
        lines.clear()

    for number, line in enumerate(text.splitlines(), 1):
        match = HEADING.fullmatch(line) if markdown else None
        if match:
            flush()
            level = len(match[1])
            headings[:] = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, match[2]))
        elif line.strip():
            lines.append((number, line))
        else:
            flush()
    flush()
    return chunks
