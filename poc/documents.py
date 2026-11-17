import hashlib
import re
from dataclasses import dataclass

CHUNK_CHARS = 320
CHUNK_OVERLAP = 40
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
IDENTIFIER = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,}(?:[-_][A-Za-z]+)*[-_]?\d+[A-Za-z0-9]*(?![A-Za-z0-9])")


@dataclass(frozen=True)
class Chunk:
    heading: str
    body: str
    line_start: int
    line_end: int


def identifiers(text: str) -> list[str]:
    return sorted({item.upper() for item in IDENTIFIER.findall(text)})


def split_document(filename: str, content: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    headings: list[tuple[int, str]] = []
    lines: list[tuple[int, str]] = []

    def flush() -> None:
        if not lines:
            return
        text = "\n".join(line for _, line in lines)
        offsets = [number for number, line in lines for _ in range(len(line) + 1)]
        heading = " / ".join(title for _, title in headings)
        for start in range(0, len(text), CHUNK_CHARS - CHUNK_OVERLAP):
            end = min(start + CHUNK_CHARS, len(text))
            if text[start:end].strip():
                chunks.append(Chunk(heading, text[start:end], offsets[start], offsets[end - 1]))
            if end == len(text):
                break
        lines.clear()

    for number, line in enumerate(content.splitlines(), 1):
        match = HEADING.fullmatch(line) if filename.lower().endswith(".md") else None
        if match:
            flush()
            level = len(match[1])
            headings = [(depth, title) for depth, title in headings if depth < level]
            headings.append((level, match[2]))
        elif line.strip():
            lines.append((number, line))
        else:
            flush()
    flush()
    return chunks


def fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def passage(heading: str, body: str) -> str:
    return f"{heading[:120]}\n{body}" if heading else body
