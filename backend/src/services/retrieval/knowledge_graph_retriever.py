"""Authoritative Knowledge Graph Retriever and Concurrency Controller.

Enforces:
- Hard caller deadlines across graph traversal and chunk hydration (default 75ms).
- Explicit admission and queue bounding with immediate load shedding (max 4 workers, max 8 queue).
- Worker capacity retention until underlying storage thread actually finishes.
- Deeply immutable PinnedSnapshotContext binding query-time document versions.
- Dynamic source and workspace tombstone checks throughout traversal and hydration.
- Bounded 1-hop and 2-hop traversal with cycle detection (max 25 nodes, max 10 edges per node).
- Targeted version-aware batch chunk hydration (capped at 15 items), with zero get_all_chunks calls.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import re
import threading
import time
from typing import Any, Callable, Sequence

from config import settings

try:
    from src.schemas.contracts.knowledge_graph import (
        ChunkLookupKey,
        KnowledgeGraphEntity,
        KnowledgeGraphLookupResult,
        KnowledgeGraphManifest,
        KnowledgeGraphRelationship,
        PinnedDocumentVersion,
        PinnedSnapshotContext,
        normalize_mention,
    )
    from src.schemas.contracts.location import TableLocation
    from src.schemas.models import Chunk
    from src.db.knowledge_graph_repo import (
        KnowledgeGraphRepositoryBase,
        build_pinned_snapshot_context,
        get_knowledge_graph_repository,
    )
    from src.modules.documents.documents_repository import DocumentsRepository
except ImportError:
    from schemas.contracts.knowledge_graph import (
        ChunkLookupKey,
        KnowledgeGraphEntity,
        KnowledgeGraphLookupResult,
        KnowledgeGraphManifest,
        KnowledgeGraphRelationship,
        PinnedDocumentVersion,
        PinnedSnapshotContext,
        normalize_mention,
    )
    from schemas.contracts.location import TableLocation
    from schemas.models import Chunk
    from db.knowledge_graph_repo import (
        KnowledgeGraphRepositoryBase,
        build_pinned_snapshot_context,
        get_knowledge_graph_repository,
    )
    from modules.documents.documents_repository import DocumentsRepository

logger = logging.getLogger(__name__)

_CAPITALIZED_ENTITY_PATTERN = re.compile(
    r"\b[A-Z][a-zA-Z0-9]*(?:\s+[A-Z][a-zA-Z0-9]*)*\b"
)

_STOP_WORDS = {
    "what", "when", "where", "who", "how", "tell", "is", "are", "the", "for",
    "under", "in", "on", "at", "to", "of", "with", "by", "according", "about",
    "from", "during", "before", "after", "per", "which", "whose", "does", "have",
    "show", "give", "can", "could", "should", "would", "an", "a", "find",
    "search", "lookup", "get", "query", "connections", "targets", "partner",
}


def extract_candidate_entity_mentions(query: str) -> list[str]:
    """Extract candidate entity mentions from query string deterministically with 0 model calls."""
    if not query:
        return []
    candidates: list[str] = []

    # 1. Capitalized words/phrases
    matches = _CAPITALIZED_ENTITY_PATTERN.findall(query)
    for m in matches:
        tokens = [t for t in m.strip().split() if t]
        while tokens and tokens[0].lower() in _STOP_WORDS:
            tokens.pop(0)
        while tokens and tokens[-1].lower() in _STOP_WORDS:
            tokens.pop()
        if not tokens:
            continue
        cleaned = " ".join(tokens)
        if len(cleaned) >= 2 and cleaned.lower() not in _STOP_WORDS:
            candidates.append(cleaned)

        # Multi-token spans: also extract sub-phrases and individual tokens
        if len(tokens) > 2:
            for i in range(len(tokens) - 1):
                span2 = " ".join(tokens[i : i + 2])
                if span2.lower() not in _STOP_WORDS:
                    candidates.append(span2)
            for tok in tokens:
                if len(tok) >= 2 and tok.lower() not in _STOP_WORDS:
                    candidates.append(tok)
        elif len(tokens) == 2:
            for tok in tokens:
                if len(tok) >= 2 and tok.lower() not in _STOP_WORDS:
                    candidates.append(tok)

    # 2. Quoted entities
    quoted = re.findall(r'"([^"]+)"', query)
    for q in quoted:
        if q.strip():
            candidates.append(q.strip())

    # 3. If no entities extracted, treat significant tokens as candidates
    if not candidates:
        tokens = [t.strip() for t in query.split() if len(t.strip()) > 3]
        filtered = [t for t in tokens if t.lower() not in _STOP_WORDS]
        candidates.extend(filtered[:5])

    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        low = c.lower()
        if low not in seen:
            seen.add(low)
            deduped.append(c)

    return deduped


class GraphConcurrencyController:
    """Admission gate and worker capacity tracker for Knowledge Graph operations.
    
    Enforces caller deadlines while retaining worker capacity until underlying
    storage threads finish executing, preventing queue saturation from orphaned tasks.
    """

    def __init__(
        self,
        max_concurrency: int | None = None,
        max_queue: int | None = None,
    ) -> None:
        self.max_concurrency = max_concurrency or getattr(settings, "GRAPH_MAX_CONCURRENCY", 4)
        self.max_queue = max_queue or getattr(settings, "GRAPH_MAX_QUEUE", 8)
        self._active_workers = 0
        self._queued_callers = 0
        self._sync_semaphore = threading.Semaphore(self.max_concurrency)
        self._thread_lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="kg_worker",
        )

    @property
    def active_count(self) -> int:
        with self._thread_lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._thread_lock:
            return self._queued_callers

    def _release_worker_slot_sync(self) -> None:
        with self._thread_lock:
            self._active_workers = max(0, self._active_workers - 1)
        self._sync_semaphore.release()

    def run_storage_work_sync(
        self,
        func: Callable[..., Any],
        *args: Any,
        deadline_s: float = 0.075,
    ) -> tuple[Any, str]:
        """Execute blocking storage work synchronously with retained capacity and caller deadlines.
        
        Returns (result, status) where status is one of:
          - "completed"
          - "caller_timeout"
          - "admission_rejected"
          - "storage_failed"
        """
        # 1. Bounded admission check
        with self._thread_lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "kg.admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()

        # 2. Acquire concurrency slot within deadline
        acquired = self._sync_semaphore.acquire(timeout=deadline_s)
        if not acquired:
            with self._thread_lock:
                self._queued_callers -= 1
            logger.warning(
                "kg.timeout_ms=%.1f fallback=baseline stage=queue_admission",
                deadline_s * 1000.0,
            )
            return None, "caller_timeout"

        with self._thread_lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, deadline_s - elapsed)

        outcome: dict[str, Any] = {"result": None, "status": "running", "error": None}
        storage_done = threading.Event()

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, remaining_s)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "storage_failed"
                logger.warning("kg.storage_error: %s", exc)
            finally:
                # Capacity is retained until the thread actually finishes
                self._release_worker_slot_sync()
                storage_done.set()

        self._executor.submit(_worker_task)

        # 3. Wait for storage task completion within remaining deadline
        finished = storage_done.wait(timeout=remaining_s)
        if finished:
            return outcome["result"], outcome["status"]

        logger.warning(
            "kg.timeout_ms=%.1f fallback=baseline stage=storage_execution",
            deadline_s * 1000.0,
        )
        return None, "caller_timeout"


def _verify_table_document_binding(
    table_store: Any,
    workspace_id: str,
    table_id: str,
    document_id: str,
    document_version: str,
) -> bool:
    """Validate durable binding between table_id, workspace_id, document_id, and document_version."""
    if not table_store or not workspace_id or not table_id or not document_id or not document_version:
        return False

    # 1. Check ingestion manifest if available
    if hasattr(table_store, "get_ingestion_manifest"):
        try:
            manifest = table_store.get_ingestion_manifest(document_id, workspace_id, version=document_version)
            if manifest is not None:
                m_tid = manifest.get("table_id")
                m_doc = manifest.get("document_id")
                m_ver = manifest.get("document_version") or manifest.get("version")
                m_ws = manifest.get("workspace_id")
                # Missing identity fields fail closed
                if not m_tid or not m_doc or not m_ver:
                    return False
                if m_tid != table_id or m_doc != document_id or str(m_ver) != str(document_version):
                    return False
                if m_ws and m_ws != workspace_id:
                    return False
                return True
        except Exception:
            pass

    # 2. Check MemoryTableStore internal schemas / active_meta
    if hasattr(table_store, "_schemas"):
        key = (workspace_id, table_id, str(document_version))
        if key in table_store._schemas:
            schema_entry = table_store._schemas[key]
            if len(schema_entry) >= 3:
                doc_id_schema = schema_entry[1]
                doc_ver_schema = schema_entry[2]
                if doc_id_schema and doc_ver_schema:
                    if doc_id_schema == document_id and str(doc_ver_schema) == str(document_version):
                        return True
            return False

    if hasattr(table_store, "_active_meta"):
        meta = table_store._active_meta.get((workspace_id, table_id))
        if meta:
            meta_doc = meta.get("document_id")
            meta_ver = meta.get("version") or meta.get("document_version")
            meta_tid = meta.get("table_id")
            meta_ws = meta.get("workspace_id")
            if not meta_doc or not meta_ver:
                return False
            if meta_doc != document_id or str(meta_ver) != str(document_version):
                return False
            if meta_tid and meta_tid != table_id:
                return False
            if meta_ws and meta_ws != workspace_id:
                return False
            return True

    # 3. Check SQLite connection tables (versioned_schemas, active_versions)
    if hasattr(table_store, "_conn"):
        try:
            lock = getattr(table_store, "_lock", None)
            from contextlib import nullcontext
            lock_ctx = lock if lock is not None else nullcontext()
            with lock_ctx:
                cur = table_store._conn.execute(
                    "SELECT workspace_id, table_id, document_id, version FROM versioned_schemas WHERE workspace_id = ? AND table_id = ? AND version = ?",
                    (workspace_id, table_id, str(document_version)),
                )
                row = cur.fetchone()
                if row:
                    doc_id = row["document_id"] if hasattr(row, "__getitem__") else getattr(row, "document_id", None)
                    ver = row["version"] if hasattr(row, "__getitem__") else getattr(row, "version", None)
                    tid = row["table_id"] if hasattr(row, "__getitem__") else getattr(row, "table_id", None)
                    ws = row["workspace_id"] if hasattr(row, "__getitem__") else getattr(row, "workspace_id", None)
                    if not doc_id or not ver or not tid or not ws:
                        return False
                    if doc_id == document_id and str(ver) == str(document_version) and tid == table_id and ws == workspace_id:
                        return True
                    return False

                cur = table_store._conn.execute(
                    "SELECT workspace_id, table_id, document_id, version FROM active_versions WHERE workspace_id = ? AND table_id = ? AND version = ?",
                    (workspace_id, table_id, str(document_version)),
                )
                row = cur.fetchone()
                if row:
                    doc_id = row["document_id"] if hasattr(row, "__getitem__") else getattr(row, "document_id", None)
                    ver = row["version"] if hasattr(row, "__getitem__") else getattr(row, "version", None)
                    tid = row["table_id"] if hasattr(row, "__getitem__") else getattr(row, "table_id", None)
                    ws = row["workspace_id"] if hasattr(row, "__getitem__") else getattr(row, "workspace_id", None)
                    if not doc_id or not ver or not tid or not ws:
                        return False
                    if doc_id == document_id and str(ver) == str(document_version) and tid == table_id and ws == workspace_id:
                        return True
                    return False
        except Exception:
            pass

    # 4. Check DynamoDB table items
    if hasattr(table_store, "table"):
        try:
            pk = f"TABLE#{workspace_id}#{table_id}#V#{document_version}"
            resp = table_store.table.get_item(Key={"PK": pk, "SK": "SCHEMA"})
            item = resp.get("Item")
            if item:
                item_doc = item.get("document_id")
                item_ver = item.get("document_version") or item.get("version")
                item_tid = item.get("table_id")
                item_ws = item.get("workspace_id")
                if not item_doc or not item_ver:
                    return False
                if item_doc == document_id and str(item_ver) == str(document_version):
                    if item_tid and item_tid != table_id:
                        return False
                    if item_ws and item_ws != workspace_id:
                        return False
                    return True
                return False

            resp_act = table_store.table.get_item(Key={"PK": f"TABLE#{workspace_id}#{table_id}", "SK": "ACTIVE_VERSION"})
            item_act = resp_act.get("Item")
            if item_act:
                act_ver = item_act.get("active_version")
                act_doc = item_act.get("document_id")
                act_tid = item_act.get("table_id")
                act_ws = item_act.get("workspace_id")
                if not act_doc or not act_ver:
                    return False
                if act_doc == document_id and str(act_ver) == str(document_version):
                    if act_tid and act_tid != table_id:
                        return False
                    if act_ws and act_ws != workspace_id:
                        return False
                    return True
                return False
        except Exception:
            pass

    # 5. Legacy table check ONLY when document_version is explicitly "legacy"
    if str(document_version) == "legacy" and hasattr(table_store, "get_table"):
        try:
            tbl = table_store.get_table(table_id, workspace_id, version="legacy")
            if tbl and getattr(tbl, "document_id", None):
                return tbl.document_id == document_id
        except Exception:
            pass

    return False


def _fetch_targeted_row(
    table_store: Any,
    table_id: str,
    workspace_id: str,
    row_id: str | int | None,
    version: str,
) -> Any | None:
    """Targeted row fetch with explicit limits. If targeted interface is missing, disables fallback."""
    if not table_store or row_id is None:
        return None

    # Targeted row lookup interface
    if hasattr(table_store, "get_row"):
        try:
            return table_store.get_row(
                table_id=table_id,
                workspace_id=workspace_id,
                row_id=row_id,
                version=version,
            )
        except Exception as e:
            logger.warning("kg.targeted_row_fetch_failed: %s", e)
            return None

    # If targeted interface is missing, do not scan the table; disable fallback safely
    return None


class KnowledgeGraphRetriever:
    """Retriever executing bounded multi-hop graph expansion and targeted chunk hydration."""

    def __init__(
        self,
        repository: KnowledgeGraphRepositoryBase | None = None,
        boundary_repo: Any | None = None,
        documents_repo: DocumentsRepository | None = None,
        concurrency_controller: GraphConcurrencyController | None = None,
        table_store: Any | None = None,
    ) -> None:
        self.repository = repository or get_knowledge_graph_repository()
        self.boundary_repo = boundary_repo
        self.documents_repo = documents_repo or DocumentsRepository()
        self.concurrency_controller = concurrency_controller or GraphConcurrencyController()
        self.table_store = table_store

    def expand(
        self,
        workspace_id: str,
        query: str,
        snapshot_context: PinnedSnapshotContext | None = None,
        max_hops: int = 2,
        caller_deadline_ms: float | None = None,
    ) -> KnowledgeGraphLookupResult:
        """Execute bounded graph retrieval under concurrency controller and deadline protection."""
        deadline_ms = caller_deadline_ms or getattr(settings, "GRAPH_TIMEOUT_MS", 75)
        deadline_s = max(0.005, deadline_ms / 1000.0)

        t_start = time.perf_counter()

        def _do_expand(rem_s: float) -> KnowledgeGraphLookupResult:
            return self._execute_expansion(
                workspace_id=workspace_id,
                query=query,
                snapshot_context=snapshot_context,
                max_hops=max_hops,
                t_start=t_start,
                rem_s=rem_s,
            )

        result, status = self.concurrency_controller.run_storage_work_sync(_do_expand, deadline_s=deadline_s)
        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        if status == "caller_timeout":
            return KnowledgeGraphLookupResult(
                execution_status="caller_timeout",
                was_truncated=True,
                truncation_reason="deadline_approaching",
                latency_ms=elapsed_ms,
            )
        elif status == "admission_rejected":
            return KnowledgeGraphLookupResult(
                execution_status="admission_rejected",
                was_truncated=True,
                truncation_reason="admission_queue_full",
                latency_ms=elapsed_ms,
            )
        elif status == "storage_failed" or result is None:
            return KnowledgeGraphLookupResult(
                execution_status="storage_failed",
                latency_ms=elapsed_ms,
            )

        return result

    def _execute_expansion(
        self,
        workspace_id: str,
        query: str,
        snapshot_context: PinnedSnapshotContext | None,
        max_hops: int,
        t_start: float,
        rem_s: float | None = None,
    ) -> KnowledgeGraphLookupResult:
        # 1. Dynamic workspace tombstone check
        if self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
            if self.boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
                return KnowledgeGraphLookupResult(
                    execution_status="tombstoned",
                    latency_ms=(time.perf_counter() - t_start) * 1000.0,
                )

        # 2. Acquire or validate PinnedSnapshotContext
        context = snapshot_context
        if context is None:
            context = build_pinned_snapshot_context(
                workspace_id=workspace_id,
                boundary_repo=self.boundary_repo,
                kg_repo=self.repository,
            )

        if not context.active_documents:
            return KnowledgeGraphLookupResult(
                execution_status="completed",
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
            )

        # 3. Seed entity resolution
        candidate_mentions = extract_candidate_entity_mentions(query)
        if not candidate_mentions:
            return KnowledgeGraphLookupResult(
                execution_status="completed",
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
            )

        mention_records, was_truncated, trunc_reason = self.repository.resolve_mentions(
            workspace_id=workspace_id,
            candidate_tokens=candidate_mentions,
            limit=10,
            max_pages=3,
        )

        # Validate seed candidates against pinned snapshot context and dynamic source tombstones
        valid_seed_records: list[dict[str, Any]] = []
        for m in mention_records:
            doc_id = m.get("document_id")
            pub_id = m.get("publication_id")
            if not doc_id or not pub_id:
                continue

            # Must come from active ready publication pinned at query start
            if not context.is_active_publication(doc_id, pub_id):
                continue

            # Dynamic source tombstone check
            if self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
                if self.boundary_repo.has_tombstone(workspace_id, "source", doc_id):
                    continue

            valid_seed_records.append(m)

        if not valid_seed_records:
            return KnowledgeGraphLookupResult(
                execution_status="completed",
                was_truncated=was_truncated,
                truncation_reason=trunc_reason,
                latency_ms=(time.perf_counter() - t_start) * 1000.0,
            )

        # Check entity ambiguity
        distinct_entity_ids = {r["entity_id"] for r in valid_seed_records if "entity_id" in r}
        distinct_types = {r.get("entity_type") for r in valid_seed_records if "entity_type" in r}
        entity_state = "ambiguous" if len(distinct_entity_ids) > 1 or len(distinct_types) > 1 else "unique"

        # 4. Multi-hop Breadth-First Traversal
        visited_nodes: set[str] = set()
        collected_edges: list[KnowledgeGraphRelationship] = []
        collected_edge_keys: set[str] = set()
        chunk_keys_map: dict[str, ChunkLookupKey] = {}
        chunk_priority_map: dict[str, tuple[float, int, str]] = {}
        conflicting_edges = False

        # Hop 1 Expansion
        current_frontier = list(distinct_entity_ids)
        next_frontier: list[tuple[str, float]] = []  # (entity_id, confidence)

        for seed_id in current_frontier:
            visited_nodes.add(seed_id)

            # Query outgoing edges
            out_edges, out_trunc, out_r = self.repository.get_outgoing_edges(
                workspace_id=workspace_id,
                source_entity_id=seed_id,
                limit=getattr(settings, "GRAPH_MAX_EDGES_PER_NODE", 10),
                max_pages=3,
            )
            if out_trunc:
                was_truncated = True
                trunc_reason = out_r or "edge_cap_reached"

            # Query incoming edges (GSI1)
            in_edges, in_trunc, in_r = self.repository.get_incoming_edges(
                workspace_id=workspace_id,
                target_entity_id=seed_id,
                limit=getattr(settings, "GRAPH_MAX_EDGES_PER_NODE", 10),
                max_pages=3,
            )
            if in_trunc:
                was_truncated = True
                trunc_reason = in_r or "edge_cap_reached"

            all_cand_edges = out_edges + in_edges
            for edge in all_cand_edges:
                # Dynamic source tombstone check
                if self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
                    if self.boundary_repo.has_tombstone(workspace_id, "source", edge.source_document_id):
                        continue

                # Pinned snapshot context check
                if not context.is_active_publication(edge.source_document_id, edge.publication_id):
                    continue

                if edge.relationship_id not in collected_edge_keys:
                    collected_edge_keys.add(edge.relationship_id)
                    collected_edges.append(edge)

                    # Collect chunk IDs for hydration
                    for cid in edge.source_chunk_ids:
                        if cid not in chunk_keys_map:
                            chunk_keys_map[cid] = ChunkLookupKey(
                                chunk_id=cid,
                                document_id=edge.source_document_id,
                                document_version=edge.source_document_version,
                            )
                        if cid not in chunk_priority_map:
                            chunk_priority_map[cid] = (-edge.confidence, 1, cid)

                    neighbor_id = edge.target_entity_id if edge.source_entity_id == seed_id else edge.source_entity_id
                    if neighbor_id not in visited_nodes:
                        next_frontier.append((neighbor_id, edge.confidence))

        # Hop 2 Expansion
        max_nodes_ceiling = getattr(settings, "GRAPH_MAX_NODES", 25)
        if rem_s is not None and (time.perf_counter() - t_start) >= rem_s:
            was_truncated = True
            trunc_reason = "deadline_approaching"
        elif max_hops >= 2 and len(visited_nodes) < max_nodes_ceiling and next_frontier:
            # Deterministic sorting: confidence DESC, entity_id ASC
            next_frontier.sort(key=lambda x: (-x[1], x[0]))
            # Deduplicate frontier
            seen_f = set()
            dedup_frontier = []
            for n_id, conf in next_frontier:
                if n_id not in seen_f and n_id not in visited_nodes:
                    seen_f.add(n_id)
                    dedup_frontier.append(n_id)

            top_frontier = dedup_frontier[:5]
            for f_node in top_frontier:
                if len(visited_nodes) >= max_nodes_ceiling:
                    was_truncated = True
                    trunc_reason = "node_cap_reached"
                    break

                visited_nodes.add(f_node)

                out_edges, _, _ = self.repository.get_outgoing_edges(
                    workspace_id=workspace_id,
                    source_entity_id=f_node,
                    limit=5,
                    max_pages=3,
                )
                in_edges, _, _ = self.repository.get_incoming_edges(
                    workspace_id=workspace_id,
                    target_entity_id=f_node,
                    limit=5,
                    max_pages=3,
                )

                for edge in out_edges + in_edges:
                    if self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
                        if self.boundary_repo.has_tombstone(workspace_id, "source", edge.source_document_id):
                            continue
                    if not context.is_active_publication(edge.source_document_id, edge.publication_id):
                        continue

                    if edge.relationship_id not in collected_edge_keys:
                        collected_edge_keys.add(edge.relationship_id)
                        collected_edges.append(edge)

                        for cid in edge.source_chunk_ids:
                            if cid not in chunk_keys_map:
                                chunk_keys_map[cid] = ChunkLookupKey(
                                    chunk_id=cid,
                                    document_id=edge.source_document_id,
                                    document_version=edge.source_document_version,
                                )
                            if cid not in chunk_priority_map:
                                chunk_priority_map[cid] = (-edge.confidence, 2, cid)

        # Detect conflicting relationships
        edge_pairs: dict[tuple[str, str], set[str]] = {}
        for edge in collected_edges:
            pair_key = (min(edge.source_entity_id, edge.target_entity_id), max(edge.source_entity_id, edge.target_entity_id))
            if pair_key not in edge_pairs:
                edge_pairs[pair_key] = set()
            edge_pairs[pair_key].add(edge.canonical_predicate)
            if len(edge_pairs[pair_key]) > 1:
                conflicting_edges = True

        relationship_state = "conflicting" if conflicting_edges else ("unique" if collected_edges else "none")

        # 5. Targeted Chunk Hydration (Batch Get, zero get_all_chunks)
        hydrated_chunks = []
        matched_chunk_ids = list(chunk_keys_map.keys())
        max_chunks_ceiling = getattr(settings, "GRAPH_MAX_HYDRATED_CHUNKS", 15)

        if chunk_keys_map:
            if rem_s is not None and (time.perf_counter() - t_start) >= rem_s:
                was_truncated = True
                trunc_reason = "deadline_approaching"
            else:
                # Deterministic sorting: confidence DESC, hop_distance ASC, chunk_id ASC
                sorted_keys = sorted(
                    chunk_keys_map.values(),
                    key=lambda k: chunk_priority_map.get(k.chunk_id, (0.0, 99, k.chunk_id)),
                )[:max_chunks_ceiling]

                raw_chunks = self.documents_repo.get_chunks_by_ids(
                    workspace_id=workspace_id,
                    chunk_keys=sorted_keys,
                )

                # Dynamic source tombstone filter on hydrated chunks
                for c in raw_chunks:
                    cid = getattr(c, "document_id", None)
                    if cid and self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
                        if self.boundary_repo.has_tombstone(workspace_id, "source", str(cid)):
                            continue
                    hydrated_chunks.append(c)

        # Grounded TableStore evidence hydration for edges with TableLocation when chunks are absent
        if len(hydrated_chunks) < max_chunks_ceiling and self.table_store is not None:
            hydrated_ids = {str(getattr(c, "id", None) or getattr(c, "chunk_id", None)) for c in hydrated_chunks}
            for edge in collected_edges:
                if len(hydrated_chunks) >= max_chunks_ceiling:
                    break
                if isinstance(edge.location, TableLocation):
                    loc = edge.location
                    if loc.row_id is None:
                        continue
                    expected_chunk_id = f"{edge.source_document_id}:table:{loc.table_id}:row{loc.row_id}"
                    if any(cid in hydrated_ids for cid in edge.source_chunk_ids) or expected_chunk_id in hydrated_ids:
                        continue
                    if self.boundary_repo and hasattr(self.boundary_repo, "has_tombstone"):
                        if self.boundary_repo.has_tombstone(workspace_id, "source", edge.source_document_id):
                            continue

                    # 1. Enforce verified document, workspace, and version bindings
                    if not _verify_table_document_binding(
                        table_store=self.table_store,
                        workspace_id=workspace_id,
                        table_id=loc.table_id,
                        document_id=edge.source_document_id,
                        document_version=edge.source_document_version,
                    ):
                        continue

                    # 2. Targeted bounded row fetch (explicit limits, no whole-table scan)
                    target_row = _fetch_targeted_row(
                        table_store=self.table_store,
                        table_id=loc.table_id,
                        workspace_id=workspace_id,
                        row_id=loc.row_id,
                        version=edge.source_document_version,
                    )
                    if target_row is None or not getattr(target_row, "cells", None):
                        # Missing row or empty cells: fail closed, return no candidate
                        continue

                    # 3. Retrieve schema to resolve column names and validate requested cell presence
                    schema = None
                    if hasattr(self.table_store, "get_schema"):
                        try:
                            schema = self.table_store.get_schema(
                                table_id=loc.table_id,
                                workspace_id=workspace_id,
                                version=edge.source_document_version,
                            )
                        except Exception:
                            pass

                    col_idx_to_name = {c.col_index: c.name for c in schema.columns} if schema else {}
                    col_name_to_idx = {c.name.lower(): c.col_index for c in schema.columns} if schema else {}

                    # 4. If column_id is specified, validate exact cell presence and value
                    if loc.column_id:
                        req_col_names = [c.strip() for c in str(loc.column_id).split("->") if c.strip()]
                        cell_validation_ok = True
                        for req_col in req_col_names:
                            matched_cell = None
                            target_col_idx = col_name_to_idx.get(req_col.lower())
                            if target_col_idx is None and req_col.isdigit():
                                target_col_idx = int(req_col)

                            for cell in target_row.cells:
                                if target_col_idx is not None and cell.col_index == target_col_idx:
                                    matched_cell = cell
                                    break
                                if str(getattr(cell, "coordinate", "")).lower() == req_col.lower():
                                    matched_cell = cell
                                    break
                                if str(getattr(cell, "cell_id", "")).lower() == req_col.lower():
                                    matched_cell = cell
                                    break

                            if matched_cell is None or matched_cell.raw_value is None or str(matched_cell.raw_value).strip() == "":
                                cell_validation_ok = False
                                break

                        if not cell_validation_ok:
                            # Missing requested cell: fail closed, return no candidate
                            continue

                    # 5. Build evidence string strictly from verified retrieved cell data (never substitute edge text)
                    cell_parts = []
                    for cell in target_row.cells:
                        if cell.raw_value is not None and str(cell.raw_value).strip() != "":
                            h_name = col_idx_to_name.get(cell.col_index, f"Col {cell.col_index}")
                            cell_parts.append(f"{h_name}: {cell.raw_value}")

                    if not cell_parts:
                        # No valid cell data: fail closed, return no candidate
                        continue

                    grounded_text = ". ".join(cell_parts) + "."

                    synth_chunk = Chunk(
                        id=expected_chunk_id,
                        document_id=edge.source_document_id,
                        source_format="csv",
                        text=grounded_text,
                        element_type="row",
                        section_path=(f"{loc.table_id}.csv",),
                        location_reference=f"Table: {loc.table_id}, Row: {loc.row_id}, Column: {loc.column_id}",
                        metadata={
                            "table_id": loc.table_id,
                            "row": loc.row_id,
                            "column": loc.column_id,
                            "version": edge.source_document_version,
                            "document_version": edge.source_document_version,
                        },
                        workspace_id=workspace_id,
                    )
                    hydrated_chunks.append(synth_chunk)
                    hydrated_ids.add(expected_chunk_id)
                    if expected_chunk_id not in matched_chunk_ids:
                        matched_chunk_ids.append(expected_chunk_id)

        elapsed_ms = (time.perf_counter() - t_start) * 1000.0

        return KnowledgeGraphLookupResult(
            seed_entities=tuple(sorted(distinct_entity_ids)),
            pinned_snapshot_captured_at=context.captured_at_utc,
            entity_state=entity_state,
            relationship_state=relationship_state,
            traversed_node_count=len(visited_nodes),
            edge_match_count=len(collected_edges),
            matched_chunk_ids=tuple(matched_chunk_ids[:max_chunks_ceiling]),
            candidate_chunks=tuple(hydrated_chunks),
            was_truncated=was_truncated,
            truncation_reason=trunc_reason,
            execution_status="truncated" if was_truncated else "completed",
            latency_ms=elapsed_ms,
        )
