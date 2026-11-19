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
            # Workspace isolation and metadata validation
            c_meta = getattr(c, "metadata", None) or {}
            c_ws = getattr(c, "workspace_id", None) or c_meta.get("workspace_id")
            if not c_ws or c_ws != workspace_id:
                # Missing or conflicting workspace -> reject
                continue

            # Document ID validation
            c_doc = getattr(c, "document_id", None) or c_meta.get("document_id")
            if not c_doc or str(c_doc) in ("doc_unknown", "unknown"):
                # Unverified or missing document ID -> reject
                continue
            c_doc = str(c_doc)

            if target_set is not None and c_doc not in target_set:
                continue

            # Dynamic source tombstone check on each chunk
            if boundary_repo is not None and hasattr(boundary_repo, "has_tombstone"):
                try:
                    if boundary_repo.has_tombstone(workspace_id, "source", c_doc):
                        continue
                except Exception:
                    continue

            # Exact version validation
            c_ver = getattr(c, "document_version", None) or getattr(c, "version", None) or c_meta.get("document_version") or c_meta.get("version")
            if not c_ver:
                # Missing exact version -> reject! Never fabricate v1!
                continue
            c_ver = str(c_ver)

            # Strict PinnedSnapshotContext bindings
            pinned_doc = snapshot_context.get_document(c_doc)
            if pinned_doc is None:
                # Absent from pinned bindings -> reject!
                continue
            if getattr(pinned_doc, "is_tombstoned", False):
                # Tombstoned in snapshot -> reject!
                continue
            if c_ver != str(pinned_doc.document_version):
                # Conflicting version metadata -> reject!
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
