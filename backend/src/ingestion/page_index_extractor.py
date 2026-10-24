"""Deterministic layout extraction and target discrimination for PageIndex.

Extracts section hierarchy, definition targets, and textual cross-references
from parsed document elements without generative model calls (0 tokens, $0 cost).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from src.schemas.contracts.page_index import (
    CrossReference,
    ElementLocation,
    PageIndexDetail,
    SectionSummary,
)

logger = logging.getLogger(__name__)

# Regex for formal definition starts, e.g. "Table 1:", "Figure 2.3.", "Exhibit 4 - Details"
_CAPTION_START_PATTERN = re.compile(
    r"^(table|figure|exhibit|box|chart)\s+([a-z0-9]+(?:[.-][a-z0-9]+)*)(?:(?:\s*[:.\-–—]\s*|\s+)(.*))?$",
    re.IGNORECASE,
)

# Regex for detecting any reference mentions within body text or cells
_REFERENCE_PATTERN = re.compile(
    r"\b(table|figure|exhibit|box|chart)\s+([a-z0-9.-]+)\b",
    re.IGNORECASE,
)


def normalize_label(prefix: str, number: str) -> str:
    """Normalize label prefix and identifier to standard lowercase form (e.g. 'table 8')."""
    clean_prefix = prefix.strip().lower()
    clean_num = number.strip().rstrip(".:-").lower()
    return f"{clean_prefix} {clean_num}"


class LayoutExtractionResult:
    """Intermediate extraction result before persistence."""

    def __init__(
        self,
        sections: tuple[SectionSummary, ...],
        definitions: dict[str, tuple[ElementLocation, ...]],
        cross_references: dict[str, tuple[CrossReference, ...]],
        layout_status: str,
        total_pages: int,
    ) -> None:
        self.sections = sections
        self.definitions = definitions
        self.cross_references = cross_references
        self.layout_status = layout_status
        self.total_pages = total_pages

    @property
    def structural_elements_count(self) -> int:
        return sum(len(locs) for locs in self.definitions.values()) + len(self.sections)


def extract_layout_from_elements(
    elements: list[dict[str, Any]] | tuple[Any, ...],
    total_pages: int = 1,
    outline_items: list[tuple[str, int, int, int]] | None = None,
    document_id: str = "",
) -> LayoutExtractionResult:
    """Deterministically extract document hierarchy, definition targets, and cross-references.

    Discrimination Rules (AC-6):
    - Definition Target:
        * element_type in ("table", "figure")
        * OR element begins with formal caption pattern and element_type in ("caption", "heading", "title")
        * Outlines / bookmarks are indexed as sections.
    - Cross Reference:
        * Mentions of tables or figures inside running paragraph body text or table cells.
        * Excluded from definition targets.
    - Missing Layout:
        * If document has no outline bookmarks, table elements, or heading chunks,
          layout_status is "missing_layout".
    """
    sections_list: list[SectionSummary] = []
    definitions_dict: dict[str, list[ElementLocation]] = {}
    cross_refs_dict: dict[str, list[CrossReference]] = {}

    max_page_seen = max(1, total_pages)

    # 1. Process outline bookmarks if supplied
    if outline_items:
        for title, level, p_start, p_end in outline_items:
            clean_title = title.strip()
            if not clean_title:
                continue
            sections_list.append(
                SectionSummary(
                    title=clean_title,
                    level=max(1, min(6, int(level))),
                    page_start=max(1, int(p_start)),
                    page_end=max(max(1, int(p_start)), int(p_end)),
                )
            )

    # 2. Iterate elements
    has_layout_signals = bool(sections_list)

    for idx, el in enumerate(elements):
        # Support dicts or Chunk-like objects
        if isinstance(el, dict):
            text = str(
                el.get("content") or el.get("source") or el.get("text") or ""
            ).strip()
            el_type = str(
                el.get("type") or el.get("element_type") or "paragraph"
            ).lower()
            page_num = int(
                el.get("page_number")
                or el.get("page number")
                or el.get("page")
                or 1
            )
            raw_box = (
                el.get("bounding_box")
                or el.get("bounding box")
                or el.get("bbox")
            )
            bbox = None
            if isinstance(raw_box, dict):
                bbox = {k: float(v) for k, v in raw_box.items()}
            elif isinstance(raw_box, (list, tuple)) and len(raw_box) == 4:
                bbox = {
                    "x1": float(raw_box[0]),
                    "y1": float(raw_box[1]),
                    "x2": float(raw_box[2]),
                    "y2": float(raw_box[3]),
                }
            el_id = str(el.get("id") or f"{document_id}:elem:{idx}")
        else:
            text = str(getattr(el, "text", "") or "").strip()
            el_type = str(getattr(el, "element_type", "paragraph") or "paragraph").lower()
            page_num = int(getattr(el, "page_number", 1) or 1)
            bbox = getattr(el, "bounding_box", None)
            el_id = str(getattr(el, "id", f"{document_id}:elem:{idx}"))

        if not text:
            continue

        if page_num > max_page_seen:
            max_page_seen = page_num

        first_line = text.split("\n")[0].strip()
        caption_match = _CAPTION_START_PATTERN.match(first_line)

        # A. Definition Target discrimination
        is_definition = False
        target_label = ""
        target_title = ""

        if el_type in ("table", "figure"):
            has_layout_signals = True
            if caption_match:
                is_definition = True
                prefix, num, rem = caption_match.groups()
                target_label = normalize_label(prefix, num)
                target_title = rem.strip() if rem else ""

        elif caption_match and el_type in ("caption", "heading", "title", "paragraph"):
            has_layout_signals = True
            is_definition = True
            prefix, num, rem = caption_match.groups()
            target_label = normalize_label(prefix, num)
            target_title = rem.strip() if rem else ""

        if is_definition:
            # Map element_type to Literal["table", "figure", "heading", "caption"]
            mapped_type = "table" if el_type == "table" else ("figure" if el_type == "figure" else "caption")
            location = ElementLocation(
                element_id=el_id,
                page_number=page_num,
                element_type=mapped_type,
                label=target_label,
                title=target_title,
                bounding_box=bbox,
            )
            definitions_dict.setdefault(target_label, []).append(location)

        # B. Section headings from elements (when not an outline)
        if el_type in ("heading", "title", "section"):
            has_layout_signals = True
            if not outline_items:
                # Level can be inferred or default 1
                sections_list.append(
                    SectionSummary(
                        title=first_line[:200],
                        level=1 if el_type == "title" else 2,
                        page_start=page_num,
                        page_end=page_num,
                    )
                )

        # C. Cross-reference detection in body text and table cells
        # Only scan if not already indexed as this element's definition target
        seen_refs_in_el: set[str] = set()
        for ref_match in _REFERENCE_PATTERN.finditer(text):
            ref_label = normalize_label(ref_match.group(1), ref_match.group(2))
            if is_definition and ref_label == target_label:
                continue
            if ref_label in seen_refs_in_el:
                continue
            seen_refs_in_el.add(ref_label)
            # Body text or cell reference
            start = max(0, ref_match.start() - 40)
            end = min(len(text), ref_match.end() + 40)
            snippet = text[start:end].replace("\n", " ").strip()
            cross_refs_dict.setdefault(ref_label, []).append(
                CrossReference(
                    element_id=el_id,
                    page_number=page_num,
                    label=ref_label,
                    context=snippet,
                )
            )

    # 3. Post-process sections to set page_end if outline was not used
    if sections_list and not outline_items:
        sections_list.sort(key=lambda s: (s.page_start, s.level))
        final_sections: list[SectionSummary] = []
        for s_idx, sec in enumerate(sections_list):
            if s_idx + 1 < len(sections_list):
                next_p = sections_list[s_idx + 1].page_start
                p_end = max(sec.page_start, next_p if next_p == sec.page_start else next_p - 1)
            else:
                p_end = max(sec.page_start, max_page_seen)
            final_sections.append(
                SectionSummary(
                    title=sec.title,
                    level=sec.level,
                    page_start=sec.page_start,
                    page_end=p_end,
                )
            )
        sections_list = final_sections

    # 4. Determine layout status
    layout_status = "ready" if has_layout_signals else "missing_layout"

    frozen_definitions = {k: tuple(v) for k, v in definitions_dict.items()}
    frozen_cross_refs = {k: tuple(v) for k, v in cross_refs_dict.items()}

    return LayoutExtractionResult(
        sections=tuple(sections_list),
        definitions=frozen_definitions,
        cross_references=frozen_cross_refs,
        layout_status=layout_status,
        total_pages=max_page_seen,
    )
