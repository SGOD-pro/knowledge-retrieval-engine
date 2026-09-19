"""Manifest models for documents, sheets, and declared completeness expectations."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SheetVisibility(str, Enum):
    VISIBLE = "visible"
    HIDDEN = "hidden"
    VERY_HIDDEN = "very_hidden"


class PopulationState(str, Enum):
    POPULATED = "populated"
    EMPTY = "empty"
    METADATA_ONLY = "metadata_only"


@dataclass(frozen=True)
class SheetManifest:
    sheet_name: str
    sheet_index: int
    visibility: SheetVisibility = SheetVisibility.VISIBLE
    state: PopulationState = PopulationState.POPULATED
    row_count: int = 0
    col_count: int = 0
    table_count: int = 0
    parser_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CompletenessExpectation:
    expected_count: int
    indexed_count: int
    unsupported_skipped_count: int = 0

    @property
    def is_complete(self) -> bool:
        return self.indexed_count >= (self.expected_count - self.unsupported_skipped_count)


@dataclass(frozen=True)
class DocumentManifest:
    document_id: str
    filename: str
    source_format: str
    source_size_bytes: int
    parser_version: str
    declared_capabilities: tuple[str, ...] = ()
    detected_pages: CompletenessExpectation | None = None
    detected_sheets: tuple[SheetManifest, ...] = ()
    detected_tables: CompletenessExpectation | None = None
    detected_rows: CompletenessExpectation | None = None
    parser_warnings: tuple[str, ...] = ()
    extraction_failures: tuple[str, ...] = ()
    ocr_applied: bool = False
    created_at_utc: str = ""

    def validate_preflight(self) -> tuple[bool, list[str]]:
        """Validate that there is no unexplained loss relative to declared parser capabilities."""
        errors: list[str] = []
        if self.detected_tables and not self.detected_tables.is_complete:
            errors.append(
                f"Table loss: expected {self.detected_tables.expected_count}, "
                f"indexed {self.detected_tables.indexed_count}, "
                f"skipped {self.detected_tables.unsupported_skipped_count}"
            )
        if self.detected_rows and not self.detected_rows.is_complete:
            errors.append(
                f"Row loss: expected {self.detected_rows.expected_count}, "
                f"indexed {self.detected_rows.indexed_count}, "
                f"skipped {self.detected_rows.unsupported_skipped_count}"
            )
        if self.detected_pages and not self.detected_pages.is_complete:
            errors.append(
                f"Page loss: expected {self.detected_pages.expected_count}, "
                f"indexed {self.detected_pages.indexed_count}, "
                f"skipped {self.detected_pages.unsupported_skipped_count}"
            )
        return len(errors) == 0, errors
