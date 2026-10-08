"""Content payload models, scalar value types, and dataset references.

Enforces deep immutability, payload byte caps, elimination of untyped values,
and separation of raw formulas from executable ASTs.
"""

import json
from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ScalarPrimitive = Union[str, int, float, bool]
OrderedScalar = Union[int, float, str]
ScalarValue = Union[str, int, float, bool, None]

FormulaEvalStatus = Literal[
    "CACHED_ONLY",
    "AST_PARSED",
    "EVAL_FAILED",
    "UNSUPPORTED_SYNTAX",
]

MAX_TEXT_PAYLOAD_CHARS = 32768
MAX_CELL_STRING_CHARS = 4096
MAX_TABLE_ROWS = 50
MAX_TABLE_COLUMNS = 50
MAX_PAYLOAD_BYTES = 262144  # 256 KB


class QualifierItem(BaseModel):
    """Typed key value qualifier for evidence metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    value: ScalarValue

    @field_validator("key")
    @classmethod
    def validate_key(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Qualifier key must be a non empty string")
        return v


class DatasetReference(BaseModel):
    """Pure source reference to a registered tabular dataset in storage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    source_version: int = Field(..., ge=1)
    registered_file_id: str
    table_id: str
    total_rows: int = Field(..., ge=0)
    total_columns: int = Field(..., ge=0)
    schema_hash: str

    @field_validator("source_id", "registered_file_id", "table_id", "schema_hash")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v


class TextPayload(BaseModel):
    """Extracted text chunk payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["text"] = "text"
    text: str
    token_count: int | None = Field(default=None, ge=0)

    @field_validator("text")
    @classmethod
    def validate_text_length(cls, v: str) -> str:
        if len(v) > MAX_TEXT_PAYLOAD_CHARS:
            raise ValueError(
                f"TextPayload exceeds maximum character limit of {MAX_TEXT_PAYLOAD_CHARS} "
                f"(received {len(v)} characters)"
            )
        return v


class CellPayload(BaseModel):
    """Individual table cell payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["cell"] = "cell"
    raw_value: ScalarValue
    normalized_number: float | None = None
    formatted_string: str | None = None

    @field_validator("raw_value")
    @classmethod
    def validate_raw_value(cls, v: ScalarValue) -> ScalarValue:
        if isinstance(v, str) and len(v) > MAX_CELL_STRING_CHARS:
            raise ValueError(
                f"CellPayload raw_value string exceeds {MAX_CELL_STRING_CHARS} characters"
            )
        return v

    @field_validator("formatted_string")
    @classmethod
    def validate_formatted_string(cls, v: str | None) -> str | None:
        if v is not None and len(v) > MAX_CELL_STRING_CHARS:
            raise ValueError(
                f"CellPayload formatted_string exceeds {MAX_CELL_STRING_CHARS} characters"
            )
        return v


class TablePayload(BaseModel):
    """Bounded inline table payload (maximum 50 rows, 50 columns, 256 KB)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["table"] = "table"
    table_id: str
    headers: tuple[str, ...]
    rows: tuple[tuple[ScalarValue, ...], ...]
    row_count: int
    col_count: int

    @field_validator("headers", mode="before")
    @classmethod
    def coerce_headers(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(x) for x in v)
        return v

    @field_validator("rows", mode="before")
    @classmethod
    def coerce_rows(cls, v: Any) -> tuple[tuple[ScalarValue, ...], ...]:
        if not isinstance(v, (list, tuple)):
            return v
        coerced_rows: list[tuple[ScalarValue, ...]] = []
        for row in v:
            if not isinstance(row, (list, tuple)):
                raise ValueError("Each table row must be a sequence of scalar values")
            coerced_row: list[ScalarValue] = []
            for item in row:
                if item is not None and not isinstance(item, (str, int, float, bool)):
                    raise ValueError(
                        f"Non scalar value '{item}' of type {type(item).__name__} in table row is forbidden"
                    )
                coerced_row.append(item)
            coerced_rows.append(tuple(coerced_row))
        return tuple(coerced_rows)

    @model_validator(mode="after")
    def validate_table_bounds(self) -> "TablePayload":
        if len(self.headers) > MAX_TABLE_COLUMNS:
            raise ValueError(
                f"TablePayload headers count {len(self.headers)} exceeds maximum {MAX_TABLE_COLUMNS}"
            )
        if len(self.rows) > MAX_TABLE_ROWS:
            raise ValueError(
                f"TablePayload rows count {len(self.rows)} exceeds maximum {MAX_TABLE_ROWS}. "
                "Large tables must use DatasetRefPayload."
            )
        if self.col_count != len(self.headers):
            raise ValueError(
                f"col_count ({self.col_count}) does not match headers count ({len(self.headers)})"
            )
        if self.row_count != len(self.rows):
            raise ValueError(
                f"row_count ({self.row_count}) does not match rows count ({len(self.rows)})"
            )
        for idx, row in enumerate(self.rows):
            if len(row) != len(self.headers):
                raise ValueError(
                    f"Row at index {idx} has length {len(row)}, expected {len(self.headers)}"
                )

        # Byte size check
        serialized_bytes = len(json.dumps(self.model_dump(mode="json")).encode("utf-8"))
        if serialized_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"TablePayload serialized size {serialized_bytes} bytes exceeds "
                f"maximum limit of {MAX_PAYLOAD_BYTES} bytes (256 KB)"
            )
        return self


class DatasetRefPayload(BaseModel):
    """Pure source reference payload for large tables exceeding inline limits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["dataset_ref"] = "dataset_ref"
    dataset_ref: DatasetReference
    total_rows: int = Field(..., ge=0)
    total_columns: int = Field(..., ge=0)
    schema_hash: str

    @model_validator(mode="after")
    def sync_dataset_ref_attributes(self) -> "DatasetRefPayload":
        if self.total_rows != self.dataset_ref.total_rows:
            raise ValueError("total_rows must match dataset_ref.total_rows")
        if self.total_columns != self.dataset_ref.total_columns:
            raise ValueError("total_columns must match dataset_ref.total_columns")
        if self.schema_hash != self.dataset_ref.schema_hash:
            raise ValueError("schema_hash must match dataset_ref.schema_hash")
        return self


class AssetPayload(BaseModel):
    """Visual figure, image, or chart asset payload without inline binary data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["asset"] = "asset"
    asset_id: str
    mime_type: str
    storage_ref: str = Field(..., max_length=1024)
    ai_description: str | None = Field(default=None, max_length=4096)
    is_ai_generated_description: bool = False

    @field_validator("asset_id", "mime_type", "storage_ref")
    @classmethod
    def validate_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field must be a non empty string")
        return v


class FormulaPayload(BaseModel):
    """Raw extracted spreadsheet formula with cached value and evaluation status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["formula"] = "formula"
    raw_expression: str = Field(..., max_length=2048)
    cached_value: ScalarValue
    eval_status: FormulaEvalStatus = "CACHED_ONLY"

    @field_validator("raw_expression")
    @classmethod
    def validate_raw_expression(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("raw_expression must be a non empty string")
        return v


ContentPayload = Annotated[
    Union[
        TextPayload,
        CellPayload,
        TablePayload,
        DatasetRefPayload,
        AssetPayload,
        FormulaPayload,
    ],
    Field(discriminator="kind"),
]


class ExecutableFormulaAST(BaseModel):
    """Server controlled executable abstract syntax tree with allowlisted operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator: Literal[
        "sum",
        "avg",
        "min",
        "max",
        "count",
        "add",
        "subtract",
        "multiply",
        "divide",
        "ratio",
        "difference",
    ]
    operands: tuple[Union[str, int, float], ...]
    null_policy: Literal["error", "zero", "null"] = "error"

    @field_validator("operands", mode="before")
    @classmethod
    def coerce_operands(cls, v: Any) -> tuple[Union[str, int, float], ...]:
        if isinstance(v, (list, tuple)):
            coerced: list[Union[str, int, float]] = []
            for item in v:
                if isinstance(item, (int, float, str)):
                    coerced.append(item)
                else:
                    raise ValueError(f"Invalid operand type {type(item).__name__}")
            return tuple(coerced)
        return v
