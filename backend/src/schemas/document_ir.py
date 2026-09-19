"""Document Intermediate Representation (DocumentIR) and streaming IngestionSink protocol."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from schemas.evidence import EvidenceChunk
from schemas.manifest import DocumentManifest, SheetManifest
from schemas.structured_table import StructuredTable, TableRow, TableSchema


class NodeType(str, Enum):
    DOCUMENT = "document"
    WORKBOOK = "workbook"
    SHEET = "sheet"
    PAGE = "page"
    SLIDE = "slide"
    SECTION = "section"
    SUBSECTION = "subsection"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FIGURE = "figure"
    CAPTION = "caption"
    FOOTNOTE = "footnote"
    HEADER = "header"
    METADATA = "metadata"


class RelationType(str, Enum):
    PARENT_OF = "parent_of"
    CHILD_OF = "child_of"
    CONTAINS = "contains"
    CAPTION_FOR = "caption_for"
    FOOTNOTE_FOR = "footnote_for"
    SPEAKER_NOTES_FOR = "speaker_notes_for"
    CONTINUATION_OF = "continuation_of"
    REFERENCES = "references"


@dataclass(frozen=True)
class Relationship:
    source_id: str
    target_id: str
    relation_type: RelationType
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StructuralNode:
    id: str
    document_id: str
    node_type: NodeType
    title: str | None = None
    text: str = ""
    page_number: int | None = None
    sheet_name: str | None = None
    slide_number: int | None = None
    section_path: tuple[str, ...] = ()
    bounding_box: dict[str, float] | None = None
    parent_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class IngestionSink(Protocol):
    """Streaming ingestion protocol for memory-efficient document ingestion."""

    def emit_manifest(self, manifest: DocumentManifest) -> None:
        ...

    def emit_sheet_manifest(self, sheet_manifest: SheetManifest) -> None:
        ...

    def emit_node(self, node: StructuralNode) -> None:
        ...

    def emit_table_schema(self, table_id: str, schema: TableSchema) -> None:
        ...

    def emit_table_rows(self, table_id: str, rows: Iterable[TableRow]) -> None:
        ...

    def emit_evidence_chunk(self, chunk: EvidenceChunk) -> None:
        ...

    def emit_relationship(self, relationship: Relationship) -> None:
        ...


@dataclass(frozen=True)
class DocumentIR:
    """Canonical in-memory document intermediate representation (used for small files / testing)."""

    manifest: DocumentManifest
    nodes: tuple[StructuralNode, ...]
    evidence_chunks: tuple[EvidenceChunk, ...]
    tables: tuple[StructuredTable, ...]
    relationships: tuple[Relationship, ...]
    workspace_id: str = ""
