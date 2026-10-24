import re

from schemas.models import Chunk

_WEIGHTS = {
    "title": 3.0,
    "heading": 2.5,
    "section": 2.0,
    "paragraph": 1.0,
    "cell": 1.0,
    "caption": 0.8,
    "footnote": 0.75,
}


def score(chunk: Chunk, query: str) -> float:
    terms = set(re.findall(r"[\w]+", query.lower()))
    chunk_terms = set(re.findall(r"[\w]+", chunk.text.lower()))
    matches = len(terms & chunk_terms)
    depth_decay = 1 / (1 + max(0, len(chunk.section_path) - 1) * 0.1)
    base = matches * _WEIGHTS.get(chunk.element_type, 1.0) * depth_decay

    # Specific structural entity boost (e.g. Table 8, Figure 5, Note 3, Appendix II)
    target_entities = re.findall(
        r"\b(table\s+\d+|figure\s+\d+|note\s+\d+|appendix\s+[a-z0-9]+)\b",
        query.lower(),
    )
    for te in target_entities:
        if te in chunk.text.lower():
            base *= 3.0
            break

    return base


def rank(chunks: list[Chunk], query: str, limit: int = 5) -> list[Chunk]:
    return sorted(chunks, key=lambda chunk: score(chunk, query), reverse=True)[:limit]


# ---------------------------------------------------------------------------
# PageIndex extraction and durable persistence integration
# ---------------------------------------------------------------------------

import logging
from pathlib import Path
from typing import Any

from config import settings
from src.db.page_index_repo import PageIndexRepository
from src.db.workspace_boundary_repo import (
    WorkspaceBoundaryRepositoryBase,
    get_workspace_boundary_repo,
)
from src.ingestion.page_index_extractor import extract_layout_from_elements
from src.schemas.contracts.page_index import PageIndexSummary

logger = logging.getLogger(__name__)


def extract_pypdf_outline(path: Path | str) -> tuple[list[tuple[str, int, int, int]], int]:
    """Extract bookmark outline tree and page count from PDF using pypdf if available."""
    outline_items: list[tuple[str, int, int, int]] = []
    file_path = Path(path)
    total = 0
    try:
        from pypdf import PdfReader
        with open(file_path, "rb") as f:
            reader = PdfReader(f)
            total = len(reader.pages)
            raw_entries: list[tuple[str, int, int]] = []
            if reader.outline:
                def _walk(items: list[Any], level: int = 1) -> None:
                    for item in items:
                        if isinstance(item, list):
                            _walk(item, level + 1)
                        elif hasattr(item, "title"):
                            title = str(item.title)
                            try:
                                page_num = reader.get_destination_page_number(item) + 1
                            except Exception:
                                page_num = 1
                            raw_entries.append((title, level, page_num))

                _walk(reader.outline)

            # Compute page_end bounded by the start of subsequent sections
            for i, (title, level, p_start) in enumerate(raw_entries):
                p_end = total
                for next_title, next_level, next_start in raw_entries[i + 1:]:
                    if next_level <= level and next_start >= p_start:
                        p_end = max(p_start, next_start - 1)
                        break
                outline_items.append((title, level, p_start, p_end))
    except Exception as exc:
        logger.debug("pypdf outline extraction skipped for %s: %s", file_path.name, exc)
    return outline_items, total


def extract_and_persist_page_index(
    path: Path | None,
    document_id: str,
    workspace_id: str,
    document_version: str,
    chunks: tuple[Chunk, ...] | list[Chunk] | None = None,
    raw_elements: list[dict[str, Any]] | None = None,
    total_pages: int | None = None,
    boundary_repo: WorkspaceBoundaryRepositoryBase | None = None,
    page_index_repo: PageIndexRepository | None = None,
) -> PageIndexSummary | None:
    """Extract structural layout and persist PageIndex manifest.

    Preserves baseline ingestion: failures log a structured error and
    record page_index_status="failed" without raising.
    """
    if not getattr(settings, "PAGEINDEX_ENABLED", True):
        return None

    b_repo = boundary_repo or get_workspace_boundary_repo()
    pi_repo = page_index_repo or PageIndexRepository(boundary_repo=b_repo)

    try:
        outline_items: list[tuple[str, int, int, int]] = []
        calc_total_pages = total_pages or 1

        if path is not None:
            file_path = Path(path)
            if file_path.exists():
                outline_items, pypdf_pages = extract_pypdf_outline(file_path)
                if pypdf_pages > 0 and not total_pages:
                    calc_total_pages = pypdf_pages

        if chunks and not total_pages:
            max_chunk_p = max((c.page_number for c in chunks if c.page_number is not None), default=1)
            calc_total_pages = max(calc_total_pages, max_chunk_p)

        elements_to_parse = raw_elements if raw_elements is not None else (chunks or ())

        # 1. Deterministic layout extraction
        layout = extract_layout_from_elements(
            elements=elements_to_parse,
            total_pages=calc_total_pages,
            outline_items=outline_items,
            document_id=document_id,
        )

        # 2. Durable persistence (DynamoDB/SQLite + S3 overflow)
        summary = pi_repo.persist_manifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            layout=layout,
        )

        # 3. Update DocumentSnapshot readiness
        try:
            snap = b_repo.get_document_snapshot(workspace_id, document_id)
            if snap is not None:
                updated_snap = snap.model_copy(
                    update={
                        "page_index_status": summary.layout_status,
                        "page_index_elements_count": summary.structural_elements_count,
                    }
                )
                b_repo.save_document_snapshot(updated_snap)
        except Exception as snap_err:
            logger.debug("Could not update DocumentSnapshot status for %s: %s", document_id, snap_err)

        return summary

    except Exception as exc:
        logger.error(
            "page_index.enrichment_failed ws=%s doc_id=%s ver=%s error=%s (preserving baseline)",
            workspace_id,
            document_id,
            document_version,
            exc,
        )
        try:
            snap = b_repo.get_document_snapshot(workspace_id, document_id)
            if snap is not None:
                updated_snap = snap.model_copy(update={"page_index_status": "failed"})
                b_repo.save_document_snapshot(updated_snap)
        except Exception:
            pass
        return None
