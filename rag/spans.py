from dataclasses import dataclass


@dataclass(frozen=True)
class SourceSpan:
    document: str
    sha256: str
    start: int
    end: int

    def covers(self, pieces: list["SourceSpan"]) -> bool:
        remaining = self.start
        for piece in sorted(pieces, key=lambda item: item.start):
            if (self.document, self.sha256) != (piece.document, piece.sha256) or piece.end <= remaining:
                continue
            if piece.start > remaining:
                return False
            remaining = piece.end
            if remaining >= self.end:
                return True
        return False
