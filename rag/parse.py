import json
import subprocess
import sys
from dataclasses import asdict, dataclass

from .format_parser import BLOCKS, PROFILES, REPORT, ParseFailure, ParseReport
from .locations import BinaryLocation
from .text import (
    CHUNK_CHARS, CHUNK_OVERLAP, CONFIG, MAX_UPLOAD_BYTES, Chunk, model_passage,
)


def version_config(mime: str) -> dict:
    if mime in PROFILES:
        return CONFIG | PROFILES[mime] | {"max_upload_bytes": MAX_UPLOAD_BYTES}
    return CONFIG


@dataclass(frozen=True)
class StructuredChunk:
    heading: str
    body: str
    source: BinaryLocation
    section_start: int

    def passage(self) -> str:
        return model_passage(self.heading, self.body)

    def location_json(self) -> dict:
        return {"source": asdict(self.source), "section_start": self.section_start}


IndexChunk = Chunk | StructuredChunk


def prepare_binary(mime: str, content: bytes, config: dict) -> tuple[list[IndexChunk], ParseReport]:
    try:
        process = subprocess.run(
            [sys.executable, "-m", "rag.format_parser", mime], input=content, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={}, timeout=config["timeout_seconds"], check=False,
        )
    except subprocess.TimeoutExpired:
        raise ParseFailure("content_limit") from None
    if process.returncode:
        raise ParseFailure("parser_failed")
    result = json.loads(process.stdout)
    report = REPORT.validate_python(result["report"])
    if "error" in result:
        raise ParseFailure(result["error"], report)
    chunks: list[IndexChunk] = []
    length, overlap = config["chunk_chars"], config["chunk_overlap"]
    for block in BLOCKS.validate_python(result["blocks"]):
        heading = " / ".join(getattr(block.location, "section", ()))
        for start in range(0, len(block.text), length - overlap):
            end = min(start + length, len(block.text))
            body = block.text[start:end]
            if body.strip():
                chunks.append(StructuredChunk(heading, body, block.location, block.section_start))
            if end == len(block.text):
                break
    return chunks, report
