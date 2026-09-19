"""Structured table models capturing source-native values, topology, headers, and formulas."""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class InferredDtype(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    DECIMAL = "decimal"
    PERCENTAGE = "percentage"
    CURRENCY = "currency"
    DATE = "date"
    BOOLEAN = "boolean"
    NULL = "null"


class HeaderTopology(str, Enum):
    SINGLE_ROW = "single_row"
    MULTI_ROW = "multi_row"
    HIERARCHICAL = "hierarchical"
    NONE = "none"


class StructureOrigin(str, Enum):
    NATIVE = "native"        # Extracted directly from tabular format (CSV, XLSX, XLS)
    INFERRED = "inferred"    # Reconstructed from layout/bounding boxes (PDF, DOCX)
    OCR = "ocr"              # Reconstructed via optical character recognition


@dataclass(frozen=True)
class MergedRange:
    range_str: str          # e.g. "A1:D1"
    start_row: int
    start_col: int
    end_row: int
    end_col: int
    anchor_cell: str        # e.g. "A1"
    value: str = ""


@dataclass(frozen=True)
class ColumnDefinition:
    col_index: int
    name: str
    path_hierarchy: tuple[str, ...] = ()
    inferred_dtype: InferredDtype = InferredDtype.STRING
    unit: str | None = None
    sample_values: tuple[str, ...] = ()
    null_ratio: float = 0.0


@dataclass(frozen=True)
class TableSchema:
    columns: tuple[ColumnDefinition, ...]
    header_rows: tuple[int, ...]
    header_topology: HeaderTopology
    confidence: float


@dataclass(frozen=True)
class TableCell:
    cell_id: str
    row_index: int
    col_index: int
    coordinate: str               # e.g. "C4"
    raw_value: Any
    cached_value: Any = None      # Evaluated/displayed value from spreadsheet
    raw_formula: str | None = None # e.g. "=SUM(C2:C3)"
    normalized_value: str | Decimal | None = None
    inferred_dtype: InferredDtype = InferredDtype.STRING
    unit: str | None = None
    is_merged: bool = False
    anchor_cell: str | None = None


@dataclass(frozen=True)
class TableRow:
    row_id: str
    row_index: int
    cells: tuple[TableCell, ...]
    is_header: bool = False
    is_subtotal: bool = False
    is_empty: bool = False


@dataclass(frozen=True)
class StructuredTable:
    table_id: str
    document_id: str
    sheet_name: str | None
    page_number: int | None
    schema: TableSchema
    rows: tuple[TableRow, ...]
    merged_ranges: tuple[MergedRange, ...] = ()
    title: str | None = None
    caption: str | None = None
    context_node_id: str | None = None
    row_count: int = 0
    col_count: int = 0
    # Provenance and fidelity of table structure
    structure_origin: StructureOrigin = StructureOrigin.NATIVE
    structure_confidence: float = 1.0
    cell_alignment_confidence: float | None = 1.0
    extraction_confidence: float | None = 1.0
