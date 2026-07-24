from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Chunk:
    id: str
    document_id: str
    source_format: str
    text: str
    element_type: str
    page_number: int | None = None
    section_path: tuple[str, ...] = ()
    bounding_box: dict[str, float] | None = None
    location_reference: str | None = None
    metadata: dict[str, Any] | None = None
    structural_weight: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["section_path"] = list(self.section_path)
        return value


@dataclass(frozen=True)
class Document:
    id: str
    filename: str
    source_format: str
    chunks: tuple[Chunk, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "source_format": self.source_format,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
        }
