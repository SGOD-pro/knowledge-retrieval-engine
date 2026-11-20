"""Executor for LAYA validated expansion routes.

Enforces Slice 5.1 constraints:
- Bounded lexical vertical path through existing BM25 interfaces.
- Other expansion routes (dense, structural, graph, structured) explicitly unavailable.
- Dynamic workspace and document tombstone enforcement before retrieval (failing closed).
- Preservation of baseline candidates without displacement.
- Version binding to active document snapshots.
- No progress detection when expansion retrieves zero novel chunks.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

try:
    from schemas.models import Chunk
    from schemas.contracts.laya import LayaEscalationDecision
    from services.retrieval.bm25_retriever import BM25Retriever, get_cached_chunks
except ImportError:
    from src.schemas.models import Chunk
    from src.schemas.contracts.laya import LayaEscalationDecision
    from src.services.retrieval.bm25_retriever import BM25Retriever, get_cached_chunks

logger = logging.getLogger(__name__)


def validate_chunk_provenance(
    chunk: Any,
    expected_workspace_id: str,
    snapshot_context: Any | None = None,
) -> tuple[bool, str | None, str | None, str | None, str | None]:
    """Validate chunk provenance across attributes and metadata against authoritative pinned bindings.

    Compares all populated workspace, document identity, and version fields across attributes
    and metadata, rejecting any disagreement rather than picking the first non empty field.
    Preserves opaque version strings without relabeling or coercion.

    Returns:
    (is_valid, workspace_id, document_id, version, rejection_reason)
    """
    if chunk is None:
        return False, None, None, None, "null_chunk"

    c_meta = getattr(chunk, "metadata", None)
    if not isinstance(c_meta, dict):
        c_meta = {}

    # 1. Compare all populated workspace fields
    ws_attr = getattr(chunk, "workspace_id", None)
    ws_meta = c_meta.get("workspace_id")
    populated_ws = [
        str(w).strip()
        for w in (ws_attr, ws_meta)
        if w is not None and str(w).strip() != ""
    ]
    if not populated_ws:
        return False, None, None, None, "missing_workspace_id"
    c_ws = populated_ws[0]
    if any(w != c_ws for w in populated_ws):
        return False, None, None, None, "conflicting_workspace_provenance"
    if expected_workspace_id and c_ws != str(expected_workspace_id).strip():
        return False, None, None, None, "workspace_boundary_mismatch"

    # 2. Compare all populated document identity fields
    doc_attr = getattr(chunk, "document_id", None)
    doc_meta = c_meta.get("document_id")
    populated_doc = [
        str(d).strip()
        for d in (doc_attr, doc_meta)
        if d is not None and str(d).strip() != ""
    ]
    if not populated_doc:
        return False, None, None, None, "missing_document_id"
    c_doc = populated_doc[0]
    if any(d != c_doc for d in populated_doc):
        return False, None, None, None, "conflicting_document_provenance"
    if c_doc in ("doc_unknown", "unknown"):
        return False, None, None, None, "unknown_document_id"

    # 3. Compare all populated version fields (preserve exact opaque string)
    ver_attr_doc = getattr(chunk, "document_version", None)
    ver_attr_v = getattr(chunk, "version", None)
    ver_meta_doc = c_meta.get("document_version")
    ver_meta_v = c_meta.get("version")
    populated_ver = [
        str(v).strip()
        for v in (ver_attr_doc, ver_attr_v, ver_meta_doc, ver_meta_v)
        if v is not None and str(v).strip() != ""
    ]
    if not populated_ver:
        return False, None, None, None, "missing_document_version"
    c_ver = populated_ver[0]
    if any(v != c_ver for v in populated_ver):
        return False, None, None, None, "conflicting_version_provenance"

    # 4. Validate against authoritative pinned snapshot context if provided
    if snapshot_context is not None and hasattr(snapshot_context, "get_document"):
        pinned_doc = snapshot_context.get_document(c_doc)
        if pinned_doc is None:
            return False, None, None, None, "document_absent_from_pinned_snapshot"
        if getattr(pinned_doc, "is_tombstoned", False):
            return False, None, None, None, "pinned_document_tombstoned"
        pinned_ver = str(getattr(pinned_doc, "document_version", "")).strip()
        if c_ver != pinned_ver:
            return False, None, None, None, "version_mismatch_with_pinned_snapshot"
        pinned_ws = getattr(pinned_doc, "workspace_id", None)
        if pinned_ws and str(pinned_ws).strip() != c_ws:
            return False, None, None, None, "workspace_mismatch_with_pinned_snapshot"

    return True, c_ws, c_doc, c_ver, None


class LayaExpansionExecutor:
    """Dispatches validated LAYA expansion decisions to underlying retrieval adapters."""

    def __init__(self, retriever: BM25Retriever | None = None) -> None:
        self.retriever = retriever or BM25Retriever()

    def execute(
        self,
        decision: LayaEscalationDecision,
        workspace_id: str,
        baseline_chunks: list[Chunk],
        boundary_repo: Any | None = None,
        chunk_repo: Any | None = None,
        snapshot_context: Any | None = None,
        timeout_s: float | None = None,
    ) -> tuple[list[Chunk], bool, str]:
        """Execute validated expansion decision and return (novel_chunks, no_progress, status).

        Returns:
        - novel_chunks: chunks retrieved that are not already present in baseline_chunks
        - no_progress: True if expansion produced zero novel chunks
        - status: "completed", "tombstoned", "no_progress", "unavailable_route", "missing_authority", or "error"
        """
        if timeout_s is not None and timeout_s <= 0.0:
            return [], True, "timeout"

        # Dynamic tombstone check on workspace BEFORE retrieval
        if boundary_repo is not None and hasattr(boundary_repo, "has_tombstone"):
            try:
                if boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
                    logger.warning("laya.tombstone_detected workspace_id=%s (failing closed)", workspace_id)
                    return [], True, "tombstoned"
            except Exception as te:
                logger.error("laya.workspace_tombstone_error: %s (failing closed)", te)
                return [], True, "tombstoned"

        if decision.route == "none":
            return [], True, "completed"

        if decision.route != "lexical_expansion":
            logger.warning("laya.executor_route_unavailable route=%s in Slice 5.1", decision.route)
            return [], True, "unavailable_route"

        # Strict snapshot authority validation: fail closed if missing or invalid
        if snapshot_context is None or not hasattr(snapshot_context, "get_document"):
            logger.warning("laya.executor_missing_snapshot_context failing closed")
            return [], True, "missing_authority"

        # Lexical expansion path
        params = decision.parameters
        query_terms = getattr(params, "query_terms", ())
        target_doc_ids = getattr(params, "document_ids", ())

        if not query_terms:
            return [], True, "no_progress"

        # Filter out tombstoned document IDs if explicitly targeted
        active_target_docs: list[str] = []
        if target_doc_ids:
            for doc_id in target_doc_ids:
                if boundary_repo is not None and hasattr(boundary_repo, "has_tombstone"):
                    try:
                        if boundary_repo.has_tombstone(workspace_id, "source", str(doc_id)):
                            logger.info("laya.target_doc_tombstoned doc_id=%s", doc_id)
                            continue
                    except Exception:
                        continue
                active_target_docs.append(str(doc_id))
            if not active_target_docs and target_doc_ids:
                logger.warning("laya.all_target_docs_tombstoned, returning 0 candidates fail closed")
                return [], True, "tombstoned"

        # Avoid unrestricted workspace scan by targeting specified documents or snapshot context documents
        target_docs_arg: list[str] | None = None
        if active_target_docs:
            target_docs_arg = list(active_target_docs)
        elif hasattr(snapshot_context, "active_documents"):
            target_docs_arg = [
                d.document_id for d in snapshot_context.active_documents
                if not getattr(d, "is_tombstoned", False)
            ]

        if not target_docs_arg:
            logger.info("laya.no_active_documents_in_snapshot for workspace_id=%s", workspace_id)
            return [], True, "no_progress"

        # Hydrate active candidate chunks
        try:
            if chunk_repo is not None and hasattr(chunk_repo, "get_all_chunks"):
                all_chunks = get_cached_chunks(
                    workspace_id,
                    lambda: chunk_repo.get_all_chunks(document_ids=target_docs_arg, workspace_id=workspace_id),
                )
            else:
                from services.langgraph_pipeline import _get_repo
                repo = _get_repo()
                all_chunks = get_cached_chunks(
                    workspace_id,
                    lambda: repo.get_all_chunks(document_ids=target_docs_arg, workspace_id=workspace_id),
                )
        except Exception as e:
            logger.error("laya.chunk_retrieval_failed: %s", e)
            return [], True, "error"

        # Scope chunks to active workspace, exact versions, and pinned snapshot bindings
        filtered_chunks: list[Chunk] = []
        target_set = set(active_target_docs) if active_target_docs else None

        for c in all_chunks:
            is_valid, c_ws, c_doc, c_ver, reason = validate_chunk_provenance(
                c,
                expected_workspace_id=workspace_id,
                snapshot_context=snapshot_context,
            )
            if not is_valid:
                logger.info("laya.chunk_provenance_rejected chunk_id=%s reason=%s", getattr(c, "id", None), reason)
                continue

            if target_set is not None and c_doc not in target_set:
                continue

            # Dynamic source tombstone check on each chunk
            if boundary_repo is not None and hasattr(boundary_repo, "has_tombstone"):
                try:
                    if boundary_repo.has_tombstone(workspace_id, "source", c_doc):
                        continue
                except Exception:
                    continue

            filtered_chunks.append(c)

        if not filtered_chunks:
            return [], True, "no_progress"

        # Execute BM25 search across expanded terms
        combined_query = " ".join(query_terms)
        search_results = self.retriever.search(combined_query, filtered_chunks, top_k=25)

        # Candidate preservation and no-progress detection
        existing_cids = {str(c.id) for c in baseline_chunks}
        novel_chunks: list[Chunk] = []

        for c, score in search_results:
            cid_str = str(c.id)
            if cid_str not in existing_cids:
                existing_cids.add(cid_str)
                # Attach retrieval score
                try:
                    c_updated = dataclasses.replace(c, similarity_score=float(score))
                except Exception:
                    c_updated = c
                novel_chunks.append(c_updated)

        no_progress = len(novel_chunks) == 0
        status = "no_progress" if no_progress else "completed"
        return novel_chunks, no_progress, status
