"""Location models supporting document, table, slide, and section coordinates.

All locators enforce deep immutability, standard top left bounding box origin,
and full 64 bit float precision.
"""

from typing import Annotated, Any, Literal, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator


def validate_normalized_bbox(
    bbox: tuple[float, float, float, float] | None,
) -> tuple[float, float, float, float] | None:
    """Validate normalized bounding box coordinates using top left origin.

    Origin (0.0, 0.0) is the top left corner, x increases rightward, and y increases downward.
    Requires 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0.
    """
    if bbox is None:
        return None
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly 4 coordinates (x0, y0, x1, y1)")
    x0, y0, x1, y1 = bbox
    for coord in (x0, y0, x1, y1):
        if not isinstance(coord, (int, float)):
            raise ValueError(f"Coordinate {coord} must be a float or int")
        if coord < 0.0 or coord > 1.0:
            raise ValueError(f"Coordinate {coord} must be normalized between 0.0 and 1.0")
    if x0 >= x1:
        raise ValueError(f"x0 ({x0}) must be strictly less than x1 ({x1})")
    if y0 >= y1:
        raise ValueError(f"y0 ({y0}) must be strictly less than y1 ({y1})")
    return float(x0), float(y0), float(x1), float(y1)


class DocumentLocation(BaseModel):
    """Visual and structural location within a document page."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["document"] = "document"
    page: int = Field(..., ge=1, description="1 indexed page number")
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalized coordinates (x0, y0, x1, y1)"
    )
    element_id: str | None = None
    section_path: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("bbox", mode="before")
    @classmethod
    def coerce_and_validate_bbox(cls, v: Any) -> tuple[float, float, float, float] | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return validate_normalized_bbox(tuple(float(x) for x in v))  # type: ignore[arg-type]
        raise ValueError("bbox must be a sequence of 4 coordinates")

    @field_validator("section_path", mode="before")
    @classmethod
    def coerce_section_path(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(x) for x in v)
        return v


class TableLocation(BaseModel):
    """Dual visual and structural coordinates for table cells and slices."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["table"] = "table"
    page: int | None = Field(default=None, ge=1, description="1 indexed page number when available")
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalized coordinates (x0, y0, x1, y1)"
    )
    table_id: str
    row_id: str | int | None = None
    column_id: str | None = None
    sheet_name: str | None = None

    @field_validator("bbox", mode="before")
    @classmethod
    def coerce_and_validate_bbox(cls, v: Any) -> tuple[float, float, float, float] | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return validate_normalized_bbox(tuple(float(x) for x in v))  # type: ignore[arg-type]
        raise ValueError("bbox must be a sequence of 4 coordinates")


class SlideLocation(BaseModel):
    """Visual and shape location within a presentation slide."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["slide"] = "slide"
    slide_number: int = Field(..., ge=1, description="1 indexed slide number")
    shape_id: str | None = None
    bbox: tuple[float, float, float, float] | None = Field(
        default=None, description="Normalized coordinates (x0, y0, x1, y1)"
    )

    @field_validator("bbox", mode="before")
    @classmethod
    def coerce_and_validate_bbox(cls, v: Any) -> tuple[float, float, float, float] | None:
        if v is None:
            return None
        if isinstance(v, (list, tuple)):
            return validate_normalized_bbox(tuple(float(x) for x in v))  # type: ignore[arg-type]
        raise ValueError("bbox must be a sequence of 4 coordinates")


class SectionLocation(BaseModel):
    """Structural section and heading location within a text document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["section"] = "section"
    heading: str
    paragraph_index: int = Field(..., ge=0, description="0 indexed paragraph sequence")
    section_path: tuple[str, ...] = Field(default_factory=tuple)

    @field_validator("section_path", mode="before")
    @classmethod
    def coerce_section_path(cls, v: Any) -> tuple[str, ...]:
        if isinstance(v, (list, tuple)):
            return tuple(str(x) for x in v)
        return v


Location = Annotated[
    Union[DocumentLocation, TableLocation, SlideLocation, SectionLocation],
    Field(discriminator="type"),
]
