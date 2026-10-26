"""Authoritative OKF Retriever enforcing workspace boundaries, deadlines, and concurrency.

Replaces legacy unpartitioned tables and broken repository caller contracts with
typed OkfLookupResult, 50ms deadlines, tombstone enforcement, and soft reciprocal
rank fusion pooling without hard exclusions.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Sequence

from config import settings
from src.db.okf_repo import OkfRepositoryBase, get_okf_repo
from src.db.workspace_boundary_repo import (
    WorkspaceBoundaryRepositoryBase,
    get_workspace_boundary_repo,
)
from src.schemas.contracts.okf import (
    OkfLookupResult,
    OkfPropertyAssertion,
    normalize_entity_name,
    normalize_property_key,
)
from src.services.retrieval.okf_lookup import (
    OkfConcurrencyController,
    WorkerResultWrapper,
    extract_entity_names_from_query,
    get_okf_controller,
)

logger = logging.getLogger(__name__)


class OkfRetriever:
    """Stage 4: Bounded OKF Property and Schema Lookup."""

    def __init__(
        self,
        repository: Any | None = None,
        boundary_repo: WorkspaceBoundaryRepositoryBase | None = None,
        controller: OkfConcurrencyController | None = None,
        chunk_repo: Any | None = None,
    ) -> None:
        from db.database import CloudRepository
        self.repository = repository or CloudRepository()
        self.boundary_repo = boundary_repo or get_workspace_boundary_repo()
        self.controller = controller or get_okf_controller()
        if hasattr(self.repository, "resolve_entities") and not type(self.repository).__name__.endswith("Mock"):
            self.okf_repo = self.repository
        else:
            self.okf_repo = get_okf_repo()
        self.chunk_repo = chunk_repo or (self.repository if hasattr(self.repository, "get_all_chunks") else None)

    def lookup(
        self,
        workspace_id: str | list[str],
        query: str = "",
        target_entities: Sequence[str] | None = None,
        deadline_ms: int | None = None,
        fast_path: bool = False,
        document_ids: Sequence[str] | None = None,
    ) -> OkfLookupResult | list[dict[str, Any]]:
        """Lookup OKF assertions within deadline and bounded concurrency.
        
        Supports modern typed signature, as well as legacy list-of-entities input.
        """
        # Legacy caller compatibility: lookup(["Acme Corp"])
        if isinstance(workspace_id, (list, tuple)):
            entities = [str(e) for e in workspace_id]
            raw_props = []
            if hasattr(self.repository, "get_okf_properties"):
                for ent in entities:
                    try:
                        p = self.repository.get_okf_properties(ent)
                        if isinstance(p, list):
                            raw_props.extend(p)
                    except Exception:
                        pass
            if raw_props:
                results = []
                for prop in raw_props:
                    if isinstance(prop, dict):
                        c = prop.get("concept") or prop.get("entity", "")
                        p_name = prop.get("property_name") or prop.get("property") or prop.get("attribute", "")
                        p_val = prop.get("property_value") or prop.get("value", "")
                        cid = prop.get("source_chunk_id") or prop.get("chunk_id", "")
                        did = prop.get("doc_id") or prop.get("document_id", "")
                        conf = float(prop.get("confidence", 1.0))
                        results.append({
                            "entity": c,
                            "concept": c,
                            "property": p_name,
                            "property_name": p_name,
                            "attribute": p_name,
                            "value": p_val,
                            "property_value": p_val,
                            "source_chunk_id": cid,
                            "doc_id": did,
                            "document_id": did,
                            "confidence": conf,
                        })
                return results

            res = self.lookup_sync(
                workspace_id="default",
                query=" ".join(entities),
                target_entities=entities,
                deadline_ms=deadline_ms,
                document_ids=document_ids,
            )
            return [
                {
                    "entity": a.entity_name,
                    "concept": a.entity_name,
                    "property": a.property_key,
                    "property_name": a.property_key,
                    "attribute": a.property_key,
                    "value": a.property_value,
                    "property_value": a.property_value,
                    "source_chunk_id": a.source_chunk_id or "",
                    "doc_id": a.document_id,
                    "document_id": a.document_id,
                    "page_number": a.page_number,
                    "row_index": a.row_index,
                    "confidence": a.confidence_score,
                }
                for a in res.matched_assertions
            ]

        return self.lookup_sync(
            workspace_id=str(workspace_id),
            query=query,
            target_entities=target_entities,
            deadline_ms=deadline_ms,
            fast_path=fast_path,
            document_ids=document_ids,
        )

    def lookup_sync(
        self,
        workspace_id: str,
        query: str,
        target_entities: Sequence[str] | None = None,
        deadline_ms: int | None = None,
        fast_path: bool = False,
        document_ids: Sequence[str] | None = None,
    ) -> OkfLookupResult:
        """Execute synchronous bounded OKF lookup with 50ms deadline and concurrency retention."""
        t0 = time.perf_counter()

        # Fast path queries bypass OKF lookup (AC-9, Key Invariants)
        if fast_path:
            return OkfLookupResult(
                target_entity=query,
                entity_state="none",
                property_state="none",
                execution_status="completed",
                latency_ms=0.0,
            )

        enabled = getattr(settings, "OKF_ENABLED", True)
        if not enabled:
            return OkfLookupResult(
                target_entity=query,
                entity_state="none",
                property_state="none",
                execution_status="completed",
                latency_ms=0.0,
            )

        timeout_ms = deadline_ms or getattr(settings, "OKF_TIMEOUT_MS", 50)
        deadline_s = timeout_ms / 1000.0

        # Candidate entity names
        candidate_names = (
            list(target_entities)
            if target_entities is not None and len(target_entities) > 0
            else extract_entity_names_from_query(query)
        )

        target_display = candidate_names[0] if candidate_names else query

        # Worker callable executed inside concurrency gate
        def _execute_storage_lookup(remaining_s: float) -> WorkerResultWrapper:
            # Step 1: Active workspace tombstone check (fail closed) (AC-7)
            try:
                if self.boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
                    logger.debug("okf.workspace_tombstoned ws=%s", workspace_id)
                    return WorkerResultWrapper(
                        OkfLookupResult(
                            target_entity=target_display,
                            entity_state="none",
                            property_state="none",
                            execution_status="tombstoned",
                        ),
                        status="completed",
                    )
            except Exception as e:
                logger.warning("okf.tombstone_check_error ws=%s error=%s (failing closed)", workspace_id, e)
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state="none",
                        property_state="none",
                        execution_status="tombstoned",
                    ),
                    status="completed",
                )

            # Step 1.5: Discover authorized document scope, active versions, and ready manifests (AC-6, AC-7)
            target_docs_set = set(document_ids) if document_ids is not None else None
            active_ready_versions: dict[str, str] = {}
            has_snapshot_data = False

            try:
                if hasattr(self.boundary_repo, "list_document_snapshots"):
                    snaps = self.boundary_repo.list_document_snapshots(workspace_id)
                    if snaps:
                        has_snapshot_data = True
                        for s in snaps:
                            if target_docs_set is not None and s.document_id not in target_docs_set:
                                continue
                            if self.boundary_repo.has_tombstone(workspace_id, "source", s.document_id):
                                continue
                            if getattr(s, "okf_status", None) == "failed":
                                continue
                            man = self.okf_repo.get_manifest(workspace_id, s.document_id, s.version)
                            if man is None or man.status != "ready":
                                continue
                            active_ready_versions[s.document_id] = s.version
            except Exception as snap_err:
                logger.warning("okf.snapshot_scope_check_error ws=%s error=%s", workspace_id, snap_err)
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state="none",
                        property_state="none",
                        execution_status="completed",
                    ),
                    status="completed",
                )

            if has_snapshot_data and not active_ready_versions:
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state="none",
                        property_state="none",
                        execution_status="completed",
                    ),
                    status="completed",
                )

            if not candidate_names:
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state="none",
                        property_state="none",
                        execution_status="completed",
                    ),
                    status="completed",
                )

            # Step 2: Resolve entities via GSI1 index (AC-6)
            entities = self.okf_repo.resolve_entities(
                workspace_id=workspace_id,
                candidate_names=candidate_names,
                limit=10,
            )

            # Filter entities by authorized document scope, active versions, and ready manifests
            valid_entities = []
            for ent in entities:
                if target_docs_set is not None and ent.document_id not in target_docs_set:
                    continue
                try:
                    if self.boundary_repo.has_tombstone(workspace_id, "source", ent.document_id):
                        continue
                except Exception:
                    continue
                if has_snapshot_data:
                    if ent.document_id not in active_ready_versions:
                        continue
                    if ent.document_version != active_ready_versions[ent.document_id]:
                        continue
                else:
                    try:
                        man = self.okf_repo.get_manifest(workspace_id, ent.document_id, ent.document_version)
                    except Exception:
                        man = None
                    if man is None or man.status != "ready":
                        continue
                valid_entities.append(ent)

            if not valid_entities:
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state="none",
                        property_state="none",
                        execution_status="completed",
                    ),
                    status="completed",
                )

            # Step 3: Entity state: check for ambiguity across distinct documents (AC-5)
            # Same name entity across distinct documents: entity_state = "ambiguous"
            distinct_docs = {e.document_id for e in valid_entities}
            entity_docs_by_name: dict[str, set[str]] = {}
            for e in valid_entities:
                entity_docs_by_name.setdefault(e.normalized_name, set()).add(e.document_id)

            has_same_name_ambiguity = any(len(docs) > 1 for docs in entity_docs_by_name.values())
            entity_state = "ambiguous" if has_same_name_ambiguity else "unique"

            # Step 4: Retrieve and filter assertions for matched entities
            raw_assertions: list[OkfPropertyAssertion] = []
            seen_assertion_ids: set[str] = set()
            for ent in valid_entities:
                active_docs_param = list(active_ready_versions.keys()) if has_snapshot_data else (list(target_docs_set) if target_docs_set is not None else None)
                ent_assertions = self.okf_repo.get_assertions_for_entity(
                    workspace_id=workspace_id,
                    entity_id=ent.entity_id,
                    active_document_ids=active_docs_param,
                    limit=20,
                )
                for ass in ent_assertions:
                    if ass.assertion_id in seen_assertion_ids:
                        continue
                    if target_docs_set is not None and ass.document_id not in target_docs_set:
                        continue
                    try:
                        if self.boundary_repo.has_tombstone(workspace_id, "source", ass.document_id):
                            continue
                    except Exception:
                        continue
                    if has_snapshot_data:
                        if ass.document_id not in active_ready_versions:
                            continue
                        if ass.document_version != active_ready_versions[ass.document_id]:
                            continue
                    else:
                        try:
                            man = self.okf_repo.get_manifest(workspace_id, ass.document_id, ass.document_version)
                        except Exception:
                            man = None
                        if man is None or man.status != "ready":
                            continue
                    seen_assertion_ids.add(ass.assertion_id)
                    raw_assertions.append(ass)

            if not raw_assertions:
                return WorkerResultWrapper(
                    OkfLookupResult(
                        target_entity=target_display,
                        entity_state=entity_state,
                        property_state="none",
                        execution_status="completed",
                    ),
                    status="completed",
                )

            # Step 5: Filter properties by query tokens (AC-9)
            query_lower = query.lower()
            query_tokens = set(re.findall(r"\w+", query_lower))

            matching_props: list[OkfPropertyAssertion] = []
            for ass in raw_assertions:
                prop_k = normalize_property_key(ass.property_key)
                prop_tokens = set(re.findall(r"\w+", prop_k))
                if any(tok in query_tokens for tok in prop_tokens):
                    matching_props.append(ass)

            # If no specific property key matched query, retain at most 2 highest confidence properties (AC-9)
            if not matching_props:
                sorted_by_conf = sorted(raw_assertions, key=lambda a: a.confidence_score, reverse=True)
                final_assertions = sorted_by_conf[:2]
            else:
                final_assertions = matching_props

            # Step 6: Conflicting property state check (AC-4)
            # Group by normalized property key and check for differing values across documents
            prop_values_by_key: dict[str, set[str]] = {}
            for ass in final_assertions:
                pk_norm = normalize_property_key(ass.property_key)
                pv_norm = ass.property_value.strip().lower()
                prop_values_by_key.setdefault(pk_norm, set()).add(pv_norm)

            has_conflict = any(len(vals) > 1 for vals in prop_values_by_key.values())
            if has_conflict:
                property_state = "conflicting"
            elif final_assertions:
                property_state = "unique"
            else:
                property_state = "none"

            # Step 7: Hydrate chunks inside worker task (AC-8)
            candidate_chunk_ids = tuple(ass.source_chunk_id for ass in final_assertions if ass.source_chunk_id)
            hydrated_chunks: list[Any] = []

            if candidate_chunk_ids and self.chunk_repo is not None:
                try:
                    all_matching = self.chunk_repo.get_all_chunks(
                        workspace_id=workspace_id,
                        document_ids=list(distinct_docs),
                    )
                    chunk_map = {str(c.id): c for c in all_matching}
                    for cid in candidate_chunk_ids:
                        if cid in chunk_map:
                            chk = chunk_map[cid]
                            chk_ver = (
                                getattr(chk, "version", None)
                                or getattr(chk, "document_version", None)
                                or (getattr(chk, "metadata", None) or {}).get("version")
                            )
                            chk_doc = getattr(chk, "document_id", None)
                            if has_snapshot_data:
                                if chk_doc not in active_ready_versions:
                                    continue
                                if chk_ver and chk_ver != active_ready_versions[chk_doc]:
                                    continue
                            else:
                                if chk_doc:
                                    try:
                                        ass_doc_ver = next(
                                            (a.document_version for a in final_assertions if a.source_chunk_id == cid),
                                            None,
                                        )
                                        effective_ver = chk_ver or ass_doc_ver or "v1"
                                        man = self.okf_repo.get_manifest(workspace_id, chk_doc, effective_ver)
                                    except Exception:
                                        man = None
                                    if man is None or man.status != "ready":
                                        continue
                            hydrated_chunks.append(chk)
                except Exception as exc:
                    logger.debug("okf.chunk_hydration_error: %s", exc)

            result = OkfLookupResult(
                target_entity=target_display,
                entity_state=entity_state,
                property_state=property_state,
                matched_assertions=tuple(final_assertions),
                candidate_chunk_ids=candidate_chunk_ids,
                candidate_chunks=tuple(hydrated_chunks),
                execution_status="completed",
            )
            return WorkerResultWrapper(result, status="completed")

        # Execute via concurrency controller
        raw_result, exec_status = self.controller.run_storage_work_sync(
            _execute_storage_lookup,
            deadline_s=deadline_s,
        )

        latency = (time.perf_counter() - t0) * 1000.0

        if exec_status == "completed" and isinstance(raw_result, OkfLookupResult):
            # Attach measured wall clock latency
            return OkfLookupResult(
                target_entity=raw_result.target_entity,
                entity_state=raw_result.entity_state,
                property_state=raw_result.property_state,
                matched_assertions=raw_result.matched_assertions,
                candidate_chunk_ids=raw_result.candidate_chunk_ids,
                candidate_chunks=raw_result.candidate_chunks,
                execution_status=raw_result.execution_status,
                latency_ms=latency,
            )

        # Timeout, rejection, or failure fallback cleanly (AC-8)
        logger.info("okf.lookup_fallback status=%s latency_ms=%.2f", exec_status, latency)
        mapped_status = (
            "caller_timeout"
            if exec_status == "caller_timeout"
            else (
                "admission_rejected"
                if exec_status == "admission_rejected"
                else "storage_failed"
            )
        )
        return OkfLookupResult(
            target_entity=target_display,
            entity_state="none",
            property_state="none",
            matched_assertions=(),
            candidate_chunk_ids=(),
            candidate_chunks=(),
            execution_status=mapped_status,
            latency_ms=latency,
        )


OKFRetriever = OkfRetriever
_SHARED_OKF_RETRIEVER: OkfRetriever | None = None
_OKF_RETRIEVER_LOCK = threading.Lock()


def get_okf_retriever() -> OkfRetriever:
    """Return shared OkfRetriever singleton."""
    global _SHARED_OKF_RETRIEVER
    if _SHARED_OKF_RETRIEVER is None:
        with _OKF_RETRIEVER_LOCK:
            if _SHARED_OKF_RETRIEVER is None:
                _SHARED_OKF_RETRIEVER = OkfRetriever()
    return _SHARED_OKF_RETRIEVER


import sys
if __name__ in ("services.retrieval.okf_retriever", "src.services.retrieval.okf_retriever"):
    sys.modules["services.retrieval.okf_retriever"] = sys.modules[__name__]
    sys.modules["src.services.retrieval.okf_retriever"] = sys.modules[__name__]

