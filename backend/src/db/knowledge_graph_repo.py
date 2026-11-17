"""Partitioned repository for Knowledge Graph entities, relationships, manifests, and budget ledger.

Provides workspace-partitioned storage for KnowledgeGraphEntity, KnowledgeGraphRelationship,
KnowledgeGraphManifest, and KnowledgeGraphBudgetLedger with:
- Main PK: KG#{workspace_id}#ENTITY#{source_id}, SK: EDGE#{target_id}#{canonical_predicate}#DOC#{doc_id}#V#{ver}#PUB#{pub_id}
- GSI1: Incoming/reverse edges (KG#{workspace_id}#TARGET#{target_id})
- GSI2: Document version scoping (KG#{workspace_id}#DOC#{doc_id}#V#{ver})
- Lookup index: KG#{workspace_id}#LOOKUP, SK: MENTION#{normalized_mention}#ENT#{ent_id}#DOC#{doc_id}#V#{ver}#PUB#{pub_id}
- Manifest and Budget Ledger: KG#{workspace_id}#DOC#{doc_id}#V#{ver}, SK: MANIFEST / BUDGET#LEDGER

Supports in-memory test storage, durable SQLite persistence (WAL mode), and DynamoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from config import settings
try:
    from src.schemas.contracts.knowledge_graph import (
        ChunkLookupKey,
        KnowledgeGraphBudgetLedger,
        KnowledgeGraphEntity,
        KnowledgeGraphLookupResult,
        KnowledgeGraphManifest,
        KnowledgeGraphReconciliationResult,
        KnowledgeGraphRelationship,
        KnowledgeGraphReservationResult,
        PinnedDocumentVersion,
        PinnedSnapshotContext,
        canonicalize_predicate,
        escape_delimiter,
        normalize_mention,
        unescape_delimiter,
    )
    from src.schemas.contracts.location import DocumentLocation, TableLocation
except ImportError:
    from schemas.contracts.knowledge_graph import (
        ChunkLookupKey,
        KnowledgeGraphBudgetLedger,
        KnowledgeGraphEntity,
        KnowledgeGraphLookupResult,
        KnowledgeGraphManifest,
        KnowledgeGraphReconciliationResult,
        KnowledgeGraphRelationship,
        KnowledgeGraphReservationResult,
        PinnedDocumentVersion,
        PinnedSnapshotContext,
        canonicalize_predicate,
        escape_delimiter,
        normalize_mention,
        unescape_delimiter,
    )
    from schemas.contracts.location import DocumentLocation, TableLocation

logger = logging.getLogger(__name__)


def disable_client_retries(client: Any) -> None:
    """Explicitly unregister retry handlers from a boto3 client."""
    try:
        service_name = getattr(getattr(client, "meta", None), "service_model", None)
        s_name = service_name.service_name if service_name else None
        events = getattr(getattr(client, "meta", None), "events", None)
        if events is None:
            return

        if s_name:
            try:
                events.unregister(f"needs-retry.{s_name}", unique_id=f"retry-config-{s_name}")
            except Exception:
                pass

        emitter = getattr(events, "_emitter", None)
        unique_ids = list(getattr(emitter, "_unique_id_handlers", {}).keys())
        for uid in unique_ids:
            if "retry" in uid:
                event_name = f"needs-retry.{s_name}" if s_name else "needs-retry"
                try:
                    events.unregister(event_name, unique_id=uid)
                except Exception:
                    pass
    except Exception:
        pass


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


class OptimisticConcurrencyError(ValueError):
    """Raised when publication visibility commit fails optimistic concurrency validation."""


class FencingConflictError(ValueError):
    """Raised when fenced publication or ledger write fails lease or fence validation."""


@runtime_checkable
class KnowledgeGraphRepositoryBase(Protocol):
    """Protocol for partitioned knowledge graph storage operations."""

    def stage_relationships(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str,
        entities: Sequence[KnowledgeGraphEntity],
        relationships: Sequence[KnowledgeGraphRelationship],
    ) -> None:
        """Stage entities, directed edges, and mention lookup items under publication_id."""
        ...

    def commit_manifest(
        self,
        workspace_id: str,
        manifest: KnowledgeGraphManifest,
        base_publication_id: str | None = None,
        fence_token: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        """Atomically commit manifest visibility with optimistic concurrency and fencing validation."""
        ...

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphManifest | None:
        """Read committed active manifest for a document version."""
        ...

    def resolve_mentions(
        self,
        workspace_id: str,
        candidate_tokens: Sequence[str],
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        """Query mention lookup index with bounded pagination and delimiter escaping."""
        ...

    def get_outgoing_edges(
        self,
        workspace_id: str,
        source_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        """Query outgoing edges for a source entity with bounded pagination."""
        ...

    def get_incoming_edges(
        self,
        workspace_id: str,
        target_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        """Query incoming/reverse edges for a target entity via GSI1 with bounded pagination."""
        ...

    def get_edges_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str | None = None,
    ) -> list[KnowledgeGraphRelationship]:
        """Query edges for a document version via GSI2."""
        ...

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphBudgetLedger | None:
        """Read budget ledger record."""
        ...

    def save_budget_ledger(
        self,
        ledger: KnowledgeGraphBudgetLedger,
    ) -> None:
        """Save or update budget ledger record."""
        ...

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 800,
        max_calls: int = 2,
        max_total_tokens: int = 5600,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1600,
    ) -> KnowledgeGraphReservationResult:
        """Atomically reserve an extraction call permit and token allocation on BUDGET#LEDGER."""
        ...

    def reconcile_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        actual_prompt_tokens: int,
        actual_output_tokens: int,
        status: str = "active",
    ) -> KnowledgeGraphReconciliationResult:
        """Reconcile completed extraction call against durable ledger and release lease."""
        ...

    def fail_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        error_reason: str,
        prompt_tokens_spent: int = 0,
        output_tokens_spent: int = 0,
    ) -> None:
        """Record failed or timed out extraction call, releasing lease while retaining reservation."""
        ...


def _parse_location(loc_dict: dict[str, Any]) -> DocumentLocation | TableLocation:
    if "table_id" in loc_dict or loc_dict.get("type") == "table":
        return TableLocation(**loc_dict)
    return DocumentLocation(**loc_dict)


def _serialize_relationship(rel: KnowledgeGraphRelationship) -> dict[str, Any]:
    loc_dict = rel.location.model_dump()
    return {
        "relationship_id": rel.relationship_id,
        "workspace_id": rel.workspace_id,
        "source_entity_id": rel.source_entity_id,
        "source_entity_name": rel.source_entity_name,
        "predicate": rel.predicate,
        "canonical_predicate": rel.canonical_predicate,
        "target_entity_id": rel.target_entity_id,
        "target_entity_name": rel.target_entity_name,
        "directed": rel.directed,
        "confidence": rel.confidence,
        "source_document_id": rel.source_document_id,
        "source_document_version": rel.source_document_version,
        "publication_id": rel.publication_id,
        "source_chunk_ids": list(rel.source_chunk_ids),
        "location": loc_dict,
        "verbatim_evidence": rel.verbatim_evidence,
        "relationship_state": rel.relationship_state,
        "created_at_utc": rel.created_at_utc,
    }


def _deserialize_relationship(data: dict[str, Any]) -> KnowledgeGraphRelationship:
    loc_data = data["location"]
    location = _parse_location(loc_data)
    return KnowledgeGraphRelationship(
        relationship_id=data["relationship_id"],
        workspace_id=data["workspace_id"],
        source_entity_id=data["source_entity_id"],
        source_entity_name=data["source_entity_name"],
        predicate=data["predicate"],
        canonical_predicate=data["canonical_predicate"],
        target_entity_id=data["target_entity_id"],
        target_entity_name=data["target_entity_name"],
        directed=data.get("directed", True),
        confidence=float(data["confidence"]),
        source_document_id=data["source_document_id"],
        source_document_version=data["source_document_version"],
        publication_id=data["publication_id"],
        source_chunk_ids=tuple(data.get("source_chunk_ids", ())),
        location=location,
        verbatim_evidence=data["verbatim_evidence"],
        relationship_state=data.get("relationship_state", "unique"),
        created_at_utc=data["created_at_utc"],
    )


class InMemoryKnowledgeGraphRepository:
    """Thread-safe in-memory store for Knowledge Graph test isolation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Key: (PK, SK) -> dict
        self._items: dict[tuple[str, str], dict[str, Any]] = {}
        # GSI1: GSI1_PK -> list[dict]
        self._gsi1: dict[str, list[dict[str, Any]]] = {}
        # GSI2: GSI2_PK -> list[dict]
        self._gsi2: dict[str, list[dict[str, Any]]] = {}
        # LOOKUP: PK -> list[dict]
        self._lookup: dict[str, list[dict[str, Any]]] = {}

    def reset_fixtures(self) -> None:
        with self._lock:
            self._items.clear()
            self._gsi1.clear()
            self._gsi2.clear()
            self._lookup.clear()

    def stage_relationships(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str,
        entities: Sequence[KnowledgeGraphEntity],
        relationships: Sequence[KnowledgeGraphRelationship],
    ) -> None:
        with self._lock:
            # Stage edges
            for rel in relationships:
                pk = f"KG#{workspace_id}#ENTITY#{rel.source_entity_id}"
                sk = f"EDGE#{rel.target_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
                gsi1_pk = f"KG#{workspace_id}#TARGET#{rel.target_entity_id}"
                gsi1_sk = f"EDGE#{rel.source_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
                gsi2_pk = f"KG#{workspace_id}#DOC#{rel.source_document_id}#V#{rel.source_document_version}"
                gsi2_sk = f"PUB#{rel.publication_id}#EDGE#{rel.source_entity_id}#{rel.target_entity_id}#{rel.canonical_predicate}"

                item = {
                    "PK": pk,
                    "SK": sk,
                    "GSI1_PK": gsi1_pk,
                    "GSI1_SK": gsi1_sk,
                    "GSI2_PK": gsi2_pk,
                    "GSI2_SK": gsi2_sk,
                    "workspace_id": workspace_id,
                    "record_type": "edge",
                    "data": _serialize_relationship(rel),
                }
                self._items[(pk, sk)] = item

                if gsi1_pk not in self._gsi1:
                    self._gsi1[gsi1_pk] = []
                self._gsi1[gsi1_pk].append(item)

                if gsi2_pk not in self._gsi2:
                    self._gsi2[gsi2_pk] = []
                self._gsi2[gsi2_pk].append(item)

            # Stage mention lookups for entities
            for ent in entities:
                lookup_pk = f"KG#{workspace_id}#LOOKUP"
                mentions = {normalize_mention(ent.name), normalize_mention(ent.canonical_name)}
                for alias in ent.aliases:
                    mentions.add(normalize_mention(alias))

                for m in mentions:
                    if not m:
                        continue
                    esc_m = escape_delimiter(m)
                    esc_eid = escape_delimiter(ent.entity_id)
                    lookup_sk = f"MENTION#{esc_m}#ENT#{esc_eid}#DOC#{document_id}#V#{document_version}#PUB#{publication_id}"
                    m_item = {
                        "PK": lookup_pk,
                        "SK": lookup_sk,
                        "workspace_id": workspace_id,
                        "record_type": "mention",
                        "entity_id": ent.entity_id,
                        "entity_name": ent.name,
                        "canonical_name": ent.canonical_name,
                        "entity_type": ent.entity_type,
                        "document_id": document_id,
                        "document_version": document_version,
                        "publication_id": publication_id,
                        "disambiguation_token": ent.disambiguation_token,
                        "data": ent.model_dump(),
                    }
                    self._items[(lookup_pk, lookup_sk)] = m_item
                    if lookup_pk not in self._lookup:
                        self._lookup[lookup_pk] = []
                    self._lookup[lookup_pk].append(m_item)

    def commit_manifest(
        self,
        workspace_id: str,
        manifest: KnowledgeGraphManifest,
        base_publication_id: str | None = None,
        fence_token: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{manifest.document_id}#V#{manifest.document_version}"
            sk = "MANIFEST"
            existing = self._items.get((pk, sk))

            # Optimistic concurrency check
            if existing is not None:
                cur_pub = existing["data"]["publication_id"]
                if base_publication_id is not None:
                    if cur_pub != base_publication_id:
                        raise OptimisticConcurrencyError(
                            f"Publication conflict: current publication '{cur_pub}' does not match base '{base_publication_id}'"
                        )
                else:
                    if existing["data"].get("status") == "ready":
                        raise OptimisticConcurrencyError(
                            f"Publication conflict: document already has active publication '{cur_pub}'"
                        )
            else:
                if base_publication_id is not None:
                    raise OptimisticConcurrencyError(
                        f"Publication conflict: expected base publication '{base_publication_id}' but none exists"
                    )

            # Fencing check
            if fence_token is not None:
                if not worker_id or not worker_id.strip():
                    raise FencingConflictError("Mandatory non-empty worker_id required for fenced publication")
                ledger_sk = "BUDGET#LEDGER"
                ledger_item = self._items.get((pk, ledger_sk))
                if not ledger_item:
                    raise FencingConflictError("Budget ledger does not exist for fenced publication")
                ld = ledger_item["data"]
                if ld.get("fence_token") != fence_token:
                    raise FencingConflictError(
                        f"Fence mismatch: expected {fence_token}, got {ld.get('fence_token')}"
                    )
                if ld.get("lease_owner_id") != worker_id:
                    raise FencingConflictError(
                        f"Lease owner mismatch: expected {worker_id}, got {ld.get('lease_owner_id')}"
                    )
                if float(ld.get("lease_expires_at", 0.0)) < time.time():
                    raise FencingConflictError("Lease has expired for fenced publication")
                # Release lease now that publication has committed
                ld["lease_owner_id"] = None
                ld["lease_expires_at"] = 0.0
                ld["updated_at"] = _now_utc()

            # Save manifest
            self._items[(pk, sk)] = {
                "PK": pk,
                "SK": sk,
                "workspace_id": workspace_id,
                "record_type": "manifest",
                "data": manifest.model_dump(),
            }

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphManifest | None:
        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            sk = "MANIFEST"
            item = self._items.get((pk, sk))
            if not item:
                return None
            return KnowledgeGraphManifest(**item["data"])

    def resolve_mentions(
        self,
        workspace_id: str,
        candidate_tokens: Sequence[str],
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        with self._lock:
            lookup_pk = f"KG#{workspace_id}#LOOKUP"
            candidates = self._lookup.get(lookup_pk, [])
            results: list[dict[str, Any]] = []
            evaluated_count = 0
            page_capacity = limit
            max_eval = page_capacity * max_pages
            was_truncated = False
            truncation_reason = None

            norm_tokens = {normalize_mention(t) for t in candidate_tokens if t}
            matching_candidates = []
            for item in candidates:
                sk = item["SK"]
                parts = sk.split("#")
                if len(parts) >= 3 and parts[0] == "MENTION":
                    token = unescape_delimiter(parts[1])
                    if token in norm_tokens or any(token.startswith(nt) for nt in norm_tokens):
                        matching_candidates.append(item)

            matching_candidates.sort(key=lambda x: x["SK"])
            if len(matching_candidates) > max_eval:
                was_truncated = True
                truncation_reason = "mention_lookup_page_limit_reached"

            seen_sks = set()
            for item in matching_candidates[:max_eval]:
                if item["SK"] in seen_sks:
                    continue
                seen_sks.add(item["SK"])
                results.append(item)
                if len(results) >= limit:
                    break

            return results, was_truncated, truncation_reason

    def get_outgoing_edges(
        self,
        workspace_id: str,
        source_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        with self._lock:
            pk = f"KG#{workspace_id}#ENTITY#{source_entity_id}"
            matched = [
                v for k, v in self._items.items()
                if k[0] == pk and k[1].startswith("EDGE#")
            ]
            matched.sort(key=lambda x: x["SK"])

            max_eval = limit * max_pages
            evaluated = len(matched)
            was_truncated = evaluated > max_eval
            truncation_reason = "edge_lookup_page_limit_reached" if was_truncated else None

            edges = [_deserialize_relationship(m["data"]) for m in matched[:min(limit, max_eval)]]
            return edges, was_truncated, truncation_reason

    def get_incoming_edges(
        self,
        workspace_id: str,
        target_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        with self._lock:
            gsi1_pk = f"KG#{workspace_id}#TARGET#{target_entity_id}"
            matched = [
                m for m in self._gsi1.get(gsi1_pk, [])
                if m["GSI1_SK"].startswith("EDGE#")
            ]
            matched.sort(key=lambda x: x["GSI1_SK"])

            max_eval = limit * max_pages
            evaluated = len(matched)
            was_truncated = evaluated > max_eval
            truncation_reason = "edge_lookup_page_limit_reached" if was_truncated else None

            edges = [_deserialize_relationship(m["data"]) for m in matched[:min(limit, max_eval)]]
            return edges, was_truncated, truncation_reason

    def get_edges_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str | None = None,
    ) -> list[KnowledgeGraphRelationship]:
        with self._lock:
            gsi2_pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            matched = self._gsi2.get(gsi2_pk, [])
            results = []
            for m in matched:
                if publication_id:
                    if m["data"]["publication_id"] == publication_id:
                        results.append(_deserialize_relationship(m["data"]))
                else:
                    results.append(_deserialize_relationship(m["data"]))
            return results

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphBudgetLedger | None:
        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            sk = "BUDGET#LEDGER"
            item = self._items.get((pk, sk))
            if not item:
                return None
            return KnowledgeGraphBudgetLedger(**item["data"])

    def save_budget_ledger(
        self,
        ledger: KnowledgeGraphBudgetLedger,
    ) -> None:
        with self._lock:
            pk = f"KG#{ledger.workspace_id}#DOC#{ledger.document_id}#V#{ledger.document_version}"
            sk = "BUDGET#LEDGER"
            self._items[(pk, sk)] = {
                "PK": pk,
                "SK": sk,
                "workspace_id": ledger.workspace_id,
                "record_type": "budget_ledger",
                "data": ledger.model_dump(),
            }

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 800,
        max_calls: int = 2,
        max_total_tokens: int = 5600,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1600,
    ) -> KnowledgeGraphReservationResult:
        if not worker_id or not worker_id.strip():
            return KnowledgeGraphReservationResult(granted=False, reason="storage_error")
        worker_id = worker_id.strip()

        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            sk = "BUDGET#LEDGER"
            item = self._items.get((pk, sk))
            ledger = KnowledgeGraphBudgetLedger(**item["data"]) if item else None
            now = time.time()
            est_tokens = estimated_prompt_tokens + max_output_tokens
            p_thresh = max_input_tokens - estimated_prompt_tokens
            o_thresh = max_output_ceiling - max_output_tokens
            t_thresh = max_total_tokens - est_tokens

            if max_calls < 1:
                return KnowledgeGraphReservationResult(
                    granted=False,
                    reason="calls_exhausted",
                    ledger=ledger,
                )

            if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
                return KnowledgeGraphReservationResult(
                    granted=False,
                    reason="tokens_exhausted",
                    ledger=ledger,
                )

            if ledger is not None:
                if ledger.status == "completed":
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_completed",
                        ledger=ledger,
                    )
                if ledger.status == "exhausted":
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_exhausted",
                        ledger=ledger,
                    )
                if ledger.status == "failed":
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_failed",
                        ledger=ledger,
                    )
                if ledger.reserved_calls >= max_calls:
                    updated = ledger.model_copy(update={"status": "exhausted", "updated_at": _now_utc()})
                    self.save_budget_ledger(updated)
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=updated.fence_token,
                        call_number=updated.reserved_calls,
                        remaining_calls=0,
                        reason="calls_exhausted",
                        ledger=updated,
                    )
                if (
                    ledger.reserved_prompt_tokens > p_thresh
                    or ledger.reserved_output_tokens > o_thresh
                    or ledger.reserved_tokens > t_thresh
                    or ledger.spent_prompt_tokens > p_thresh
                    or ledger.spent_output_tokens > o_thresh
                    or ledger.spent_tokens > t_thresh
                ):
                    updated = ledger.model_copy(update={"status": "exhausted", "updated_at": _now_utc()})
                    self.save_budget_ledger(updated)
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=updated.fence_token,
                        call_number=updated.reserved_calls,
                        remaining_calls=0,
                        reason="tokens_exhausted",
                        ledger=updated,
                    )
                if ledger.lease_expires_at > now and ledger.lease_owner_id != worker_id:
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=max(0, max_calls - ledger.reserved_calls),
                        reason="active_lease_held",
                        ledger=ledger,
                    )

                new_calls = ledger.reserved_calls + 1
                new_fence = ledger.fence_token + 1
                new_reserved = ledger.reserved_tokens + est_tokens
                new_reserved_prompt = ledger.reserved_prompt_tokens + estimated_prompt_tokens
                new_reserved_output = ledger.reserved_output_tokens + max_output_tokens
                updated = KnowledgeGraphBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=new_calls,
                    completed_calls=ledger.completed_calls,
                    reserved_tokens=new_reserved,
                    spent_tokens=ledger.spent_tokens,
                    reserved_prompt_tokens=new_reserved_prompt,
                    reserved_output_tokens=new_reserved_output,
                    spent_prompt_tokens=ledger.spent_prompt_tokens,
                    spent_output_tokens=ledger.spent_output_tokens,
                    lease_owner_id=worker_id,
                    lease_expires_at=now + lease_duration_s,
                    fence_token=new_fence,
                    reconciled_fences=ledger.reconciled_fences,
                    status="active",
                    updated_at=_now_utc(),
                )
                self.save_budget_ledger(updated)
                return KnowledgeGraphReservationResult(
                    granted=True,
                    fence_token=new_fence,
                    call_number=new_calls,
                    remaining_calls=max_calls - new_calls,
                    reason="granted",
                    ledger=updated,
                )

            # Initial reservation
            new_calls = 1
            new_fence = 1
            updated = KnowledgeGraphBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=new_calls,
                completed_calls=0,
                reserved_tokens=est_tokens,
                spent_tokens=0,
                reserved_prompt_tokens=estimated_prompt_tokens,
                reserved_output_tokens=max_output_tokens,
                spent_prompt_tokens=0,
                spent_output_tokens=0,
                lease_owner_id=worker_id,
                lease_expires_at=now + lease_duration_s,
                fence_token=new_fence,
                reconciled_fences=(),
                status="active",
                updated_at=_now_utc(),
            )
            self.save_budget_ledger(updated)
            return KnowledgeGraphReservationResult(
                granted=True,
                fence_token=new_fence,
                call_number=new_calls,
                remaining_calls=max_calls - new_calls,
                reason="granted",
                ledger=updated,
            )

    def reconcile_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        actual_prompt_tokens: int,
        actual_output_tokens: int,
        status: str = "active",
    ) -> KnowledgeGraphReconciliationResult:
        worker_id = worker_id.strip() if worker_id else ""
        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            sk = "BUDGET#LEDGER"
            item = self._items.get((pk, sk))
            if not item:
                return KnowledgeGraphReconciliationResult(reconciled=False, reason="not_found")
            ledger = KnowledgeGraphBudgetLedger(**item["data"])

            if fence_token in ledger.reconciled_fences:
                return KnowledgeGraphReconciliationResult(reconciled=True, reason="already_reconciled", ledger=ledger)

            fenced_out = (ledger.fence_token != fence_token) or (ledger.lease_owner_id != worker_id)

            actual_total = actual_prompt_tokens + actual_output_tokens
            new_spent_prompt = ledger.spent_prompt_tokens + actual_prompt_tokens
            new_spent_output = ledger.spent_output_tokens + actual_output_tokens
            new_spent = ledger.spent_tokens + actual_total
            new_completed = ledger.completed_calls + 1
            new_rf = tuple(list(ledger.reconciled_fences) + [fence_token])

            if fenced_out:
                updated = ledger.model_copy(update={
                    "spent_tokens": new_spent,
                    "spent_prompt_tokens": new_spent_prompt,
                    "spent_output_tokens": new_spent_output,
                    "completed_calls": new_completed,
                    "reconciled_fences": new_rf,
                    "updated_at": _now_utc(),
                })
                self.save_budget_ledger(updated)
                return KnowledgeGraphReconciliationResult(
                    reconciled=False,
                    fenced_out=True,
                    reason="fenced_out",
                    ledger=updated,
                )

            updated = KnowledgeGraphBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=ledger.reserved_calls,
                completed_calls=new_completed,
                reserved_tokens=ledger.reserved_tokens,
                spent_tokens=new_spent,
                reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                reserved_output_tokens=ledger.reserved_output_tokens,
                spent_prompt_tokens=new_spent_prompt,
                spent_output_tokens=new_spent_output,
                lease_owner_id=worker_id,
                lease_expires_at=ledger.lease_expires_at,
                fence_token=ledger.fence_token,
                reconciled_fences=new_rf,
                status=status if status in ("active", "completed", "exhausted", "failed") else ledger.status,
                updated_at=_now_utc(),
            )
            self.save_budget_ledger(updated)
            return KnowledgeGraphReconciliationResult(
                reconciled=True,
                fenced_out=False,
                reason="reconciled",
                ledger=updated,
            )

    def fail_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        error_reason: str,
        prompt_tokens_spent: int = 0,
        output_tokens_spent: int = 0,
    ) -> None:
        worker_id = worker_id.strip() if worker_id else ""
        with self._lock:
            pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
            sk = "BUDGET#LEDGER"
            item = self._items.get((pk, sk))
            if not item:
                return
            ledger = KnowledgeGraphBudgetLedger(**item["data"])
            if fence_token in ledger.reconciled_fences:
                if ledger.fence_token == fence_token and ledger.lease_owner_id == worker_id:
                    updated = ledger.model_copy(update={
                        "lease_owner_id": None,
                        "lease_expires_at": 0.0,
                        "updated_at": _now_utc(),
                    })
                    self.save_budget_ledger(updated)
                return

            actual_total = prompt_tokens_spent + output_tokens_spent
            new_spent_prompt = ledger.spent_prompt_tokens + prompt_tokens_spent
            new_spent_output = ledger.spent_output_tokens + output_tokens_spent
            new_spent = ledger.spent_tokens + actual_total
            new_rf = tuple(list(ledger.reconciled_fences) + [fence_token])

            owner = ledger.lease_owner_id
            expires = ledger.lease_expires_at
            if ledger.fence_token == fence_token and owner == worker_id:
                owner = None
                expires = 0.0

            updated = ledger.model_copy(update={
                "spent_tokens": new_spent,
                "spent_prompt_tokens": new_spent_prompt,
                "spent_output_tokens": new_spent_output,
                "lease_owner_id": owner,
                "lease_expires_at": expires,
                "reconciled_fences": new_rf,
                "updated_at": _now_utc(),
            })
            self.save_budget_ledger(updated)


class SQLiteKnowledgeGraphRepository:
    """Durable SQLite storage with WAL mode for Knowledge Graph enrichment."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            configured = getattr(settings, "GRAPH_SQLITE_PATH", None)
            if configured:
                self.db_path = Path(configured)
            else:
                self.db_path = Path(__file__).resolve().parent.parent.parent / "data" / "kre_knowledge_graph.db"
        else:
            self.db_path = Path(db_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._pre_write_hook: Any | None = None
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._init_sqlite()

    def _init_sqlite(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS kre_knowledge_graph (
                        workspace_id TEXT NOT NULL,
                        pk TEXT NOT NULL,
                        sk TEXT NOT NULL,
                        gsi1_pk TEXT NOT NULL,
                        gsi1_sk TEXT NOT NULL,
                        gsi2_pk TEXT NOT NULL,
                        gsi2_sk TEXT NOT NULL,
                        record_type TEXT NOT NULL,
                        document_id TEXT NOT NULL,
                        document_version TEXT NOT NULL,
                        publication_id TEXT NOT NULL,
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (pk, sk)
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_gsi1 ON kre_knowledge_graph (gsi1_pk, gsi1_sk)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_gsi2 ON kre_knowledge_graph (gsi2_pk, gsi2_sk)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_kg_lookup ON kre_knowledge_graph (pk, sk)"
                )

    def reset_fixtures(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM kre_knowledge_graph")

    def stage_relationships(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str,
        entities: Sequence[KnowledgeGraphEntity],
        relationships: Sequence[KnowledgeGraphRelationship],
    ) -> None:
        now_str = _now_utc()
        rows = []

        for rel in relationships:
            pk = f"KG#{workspace_id}#ENTITY#{rel.source_entity_id}"
            sk = f"EDGE#{rel.target_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
            gsi1_pk = f"KG#{workspace_id}#TARGET#{rel.target_entity_id}"
            gsi1_sk = f"EDGE#{rel.source_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
            gsi2_pk = f"KG#{workspace_id}#DOC#{rel.source_document_id}#V#{rel.source_document_version}"
            gsi2_sk = f"PUB#{rel.publication_id}#EDGE#{rel.source_entity_id}#{rel.target_entity_id}#{rel.canonical_predicate}"
            data_json = json.dumps(_serialize_relationship(rel))

            rows.append((
                workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                "edge", document_id, document_version, publication_id, data_json, now_str
            ))

        for ent in entities:
            lookup_pk = f"KG#{workspace_id}#LOOKUP"
            mentions = {normalize_mention(ent.name), normalize_mention(ent.canonical_name)}
            for alias in ent.aliases:
                mentions.add(normalize_mention(alias))

            for m in mentions:
                if not m:
                    continue
                esc_m = escape_delimiter(m)
                esc_eid = escape_delimiter(ent.entity_id)
                lookup_sk = f"MENTION#{esc_m}#ENT#{esc_eid}#DOC#{document_id}#V#{document_version}#PUB#{publication_id}"
                data_json = json.dumps(ent.model_dump())
                rows.append((
                    workspace_id, lookup_pk, lookup_sk, "", "", "", "",
                    "mention", document_id, document_version, publication_id, data_json, now_str
                ))

        with self._lock:
            with self._conn:
                self._conn.executemany(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )

    def commit_manifest(
        self,
        workspace_id: str,
        manifest: KnowledgeGraphManifest,
        base_publication_id: str | None = None,
        fence_token: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        pk = f"KG#{workspace_id}#DOC#{manifest.document_id}#V#{manifest.document_version}"
        sk = "MANIFEST"
        now_str = _now_utc()
        manifest_json = json.dumps(manifest.model_dump())

        with self._lock:
            in_tx = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                in_tx = True
            except sqlite3.OperationalError:
                raise

            try:
                # Test pause hook right before authoritative in-transaction validation and write
                if self._pre_write_hook is not None:
                    self._pre_write_hook()

                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                existing_row = cursor.fetchone()

                if existing_row is not None:
                    existing_data = json.loads(existing_row["data_json"])
                    cur_pub = existing_data.get("publication_id")
                    if base_publication_id is not None:
                        if cur_pub != base_publication_id:
                            raise OptimisticConcurrencyError(
                                f"Publication conflict: current publication '{cur_pub}' does not match base '{base_publication_id}'"
                            )
                    else:
                        if existing_data.get("status") == "ready":
                            raise OptimisticConcurrencyError(
                                f"Publication conflict: document already has active publication '{cur_pub}'"
                            )
                else:
                    if base_publication_id is not None:
                        raise OptimisticConcurrencyError(
                            f"Publication conflict: expected base publication '{base_publication_id}' but none exists"
                        )

                if fence_token is not None:
                    if not worker_id or not worker_id.strip():
                        raise FencingConflictError("Mandatory non-empty worker_id required for fenced publication")
                    ledger_sk = "BUDGET#LEDGER"
                    l_cur = self._conn.execute(
                        "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                        (pk, ledger_sk),
                    )
                    l_row = l_cur.fetchone()
                    if not l_row:
                        raise FencingConflictError("Budget ledger does not exist for fenced publication")
                    ld = json.loads(l_row["data_json"])
                    if ld.get("fence_token") != fence_token:
                        raise FencingConflictError(
                            f"Fence mismatch: expected {fence_token}, got {ld.get('fence_token')}"
                        )
                    if ld.get("lease_owner_id") != worker_id:
                        raise FencingConflictError(
                            f"Lease owner mismatch: expected {worker_id}, got {ld.get('lease_owner_id')}"
                        )
                    if float(ld.get("lease_expires_at", 0.0)) < time.time():
                        raise FencingConflictError("Lease has expired for fenced publication")
                    # Release lease now that publication has committed
                    ld["lease_owner_id"] = None
                    ld["lease_expires_at"] = 0.0
                    ld["updated_at"] = now_str
                    self._conn.execute(
                        "UPDATE kre_knowledge_graph SET data_json = ? WHERE pk = ? AND sk = ?",
                        (json.dumps(ld), pk, ledger_sk),
                    )

                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id, pk, sk, "", "", "", "",
                        "manifest", manifest.document_id, manifest.document_version,
                        manifest.publication_id, manifest_json, now_str
                    ),
                )
                self._conn.execute("COMMIT")
                in_tx = False
            except Exception:
                if in_tx:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                raise

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphManifest | None:
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "MANIFEST"
        with self._lock:
            cursor = self._conn.execute(
                "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                (pk, sk),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return KnowledgeGraphManifest(**json.loads(row["data_json"]))

    def resolve_mentions(
        self,
        workspace_id: str,
        candidate_tokens: Sequence[str],
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        lookup_pk = f"KG#{workspace_id}#LOOKUP"
        norm_tokens = [normalize_mention(t) for t in candidate_tokens if t]
        if not norm_tokens:
            return [], False, None

        max_eval = limit * max_pages
        results: list[dict[str, Any]] = []
        was_truncated = False
        truncation_reason = None
        seen_sks = set()

        with self._lock:
            for token in norm_tokens:
                esc_token = escape_delimiter(token)
                prefix = f"MENTION#{esc_token}%"
                cursor = self._conn.execute(
                    """
                    SELECT sk, document_id, document_version, publication_id, data_json FROM kre_knowledge_graph
                    WHERE pk = ? AND sk LIKE ?
                    ORDER BY sk ASC
                    LIMIT ?
                    """,
                    (lookup_pk, prefix, max_eval + 1),
                )
                rows = cursor.fetchall()
                if len(rows) > max_eval:
                    was_truncated = True
                    truncation_reason = "mention_lookup_page_limit_reached"
                    rows = rows[:max_eval]

                for row in rows:
                    sk = row["sk"]
                    if sk in seen_sks:
                        continue
                    seen_sks.add(sk)
                    data = json.loads(row["data_json"])
                    results.append({
                        "SK": sk,
                        "document_id": row["document_id"],
                        "document_version": row["document_version"],
                        "publication_id": row["publication_id"],
                        **data,
                    })
                    if len(results) >= limit:
                        break
                if len(results) >= limit:
                    break

        return results, was_truncated, truncation_reason

    def get_outgoing_edges(
        self,
        workspace_id: str,
        source_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        pk = f"KG#{workspace_id}#ENTITY#{source_entity_id}"
        max_eval = 10 * max_pages
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT data_json FROM kre_knowledge_graph
                WHERE pk = ? AND sk LIKE 'EDGE#%'
                ORDER BY sk ASC
                LIMIT ?
                """,
                (pk, max_eval + 1),
            )
            rows = cursor.fetchall()
            was_truncated = len(rows) > max_eval
            truncation_reason = "edge_lookup_page_limit_reached" if was_truncated else None
            edges = [_deserialize_relationship(json.loads(r["data_json"])) for r in rows[:min(limit, max_eval)]]
            return edges, was_truncated, truncation_reason

    def get_incoming_edges(
        self,
        workspace_id: str,
        target_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        gsi1_pk = f"KG#{workspace_id}#TARGET#{target_entity_id}"
        max_eval = 10 * max_pages
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT data_json FROM kre_knowledge_graph
                WHERE gsi1_pk = ? AND gsi1_sk LIKE 'EDGE#%'
                ORDER BY gsi1_sk ASC
                LIMIT ?
                """,
                (gsi1_pk, max_eval + 1),
            )
            rows = cursor.fetchall()
            was_truncated = len(rows) > max_eval
            truncation_reason = "edge_lookup_page_limit_reached" if was_truncated else None
            edges = [_deserialize_relationship(json.loads(r["data_json"])) for r in rows[:min(limit, max_eval)]]
            return edges, was_truncated, truncation_reason

    def get_edges_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str | None = None,
    ) -> list[KnowledgeGraphRelationship]:
        gsi2_pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        with self._lock:
            if publication_id:
                cursor = self._conn.execute(
                    """
                    SELECT data_json FROM kre_knowledge_graph
                    WHERE gsi2_pk = ? AND publication_id = ?
                    ORDER BY gsi2_sk ASC
                    """,
                    (gsi2_pk, publication_id),
                )
            else:
                cursor = self._conn.execute(
                    """
                    SELECT data_json FROM kre_knowledge_graph
                    WHERE gsi2_pk = ? AND gsi2_sk LIKE 'PUB#%'
                    ORDER BY gsi2_sk ASC
                    """,
                    (gsi2_pk,),
                )
            rows = cursor.fetchall()
            return [_deserialize_relationship(json.loads(r["data_json"])) for r in rows]

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphBudgetLedger | None:
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"
        with self._lock:
            cursor = self._conn.execute(
                "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                (pk, sk),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return KnowledgeGraphBudgetLedger(**json.loads(row["data_json"]))

    def save_budget_ledger(
        self,
        ledger: KnowledgeGraphBudgetLedger,
    ) -> None:
        pk = f"KG#{ledger.workspace_id}#DOC#{ledger.document_id}#V#{ledger.document_version}"
        sk = "BUDGET#LEDGER"
        now_str = _now_utc()
        data_json = json.dumps(ledger.model_dump())
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ledger.workspace_id, pk, sk, "", "", "", "",
                        "budget_ledger", ledger.document_id, ledger.document_version,
                        "", data_json, now_str
                    ),
                )

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 800,
        max_calls: int = 2,
        max_total_tokens: int = 5600,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1600,
    ) -> KnowledgeGraphReservationResult:
        if not worker_id or not worker_id.strip():
            return KnowledgeGraphReservationResult(granted=False, reason="storage_error")
        worker_id = worker_id.strip()

        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"
        now = time.time()
        est_tokens = estimated_prompt_tokens + max_output_tokens
        p_thresh = max_input_tokens - estimated_prompt_tokens
        o_thresh = max_output_ceiling - max_output_tokens
        t_thresh = max_total_tokens - est_tokens

        with self._lock:
            in_tx = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                in_tx = True
            except sqlite3.OperationalError:
                return KnowledgeGraphReservationResult(granted=False, reason="storage_error")

            try:
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                ledger = KnowledgeGraphBudgetLedger(**json.loads(row["data_json"])) if row else None

                if max_calls < 1:
                    self._conn.execute("COMMIT")
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        reason="calls_exhausted",
                        ledger=ledger,
                    )

                if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
                    self._conn.execute("COMMIT")
                    return KnowledgeGraphReservationResult(
                        granted=False,
                        reason="tokens_exhausted",
                        ledger=ledger,
                    )

                if ledger is not None:
                    if ledger.status == "completed":
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_completed",
                            ledger=ledger,
                        )
                    if ledger.status == "exhausted":
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_exhausted",
                            ledger=ledger,
                        )
                    if ledger.status == "failed":
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_failed",
                            ledger=ledger,
                        )
                    if ledger.reserved_calls >= max_calls:
                        updated = ledger.model_copy(update={"status": "exhausted", "updated_at": _now_utc()})
                        data_json = json.dumps(updated.model_dump())
                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO kre_knowledge_graph (
                                workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                                record_type, document_id, document_version, publication_id,
                                data_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                updated.workspace_id, pk, sk, "", "", "", "",
                                "budget_ledger", updated.document_id, updated.document_version,
                                "", data_json, _now_utc()
                            ),
                        )
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=updated.fence_token,
                            call_number=updated.reserved_calls,
                            remaining_calls=0,
                            reason="calls_exhausted",
                            ledger=updated,
                        )
                    if (
                        ledger.reserved_prompt_tokens > p_thresh
                        or ledger.reserved_output_tokens > o_thresh
                        or ledger.reserved_tokens > t_thresh
                        or ledger.spent_prompt_tokens > p_thresh
                        or ledger.spent_output_tokens > o_thresh
                        or ledger.spent_tokens > t_thresh
                    ):
                        updated = ledger.model_copy(update={"status": "exhausted", "updated_at": _now_utc()})
                        data_json = json.dumps(updated.model_dump())
                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO kre_knowledge_graph (
                                workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                                record_type, document_id, document_version, publication_id,
                                data_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                updated.workspace_id, pk, sk, "", "", "", "",
                                "budget_ledger", updated.document_id, updated.document_version,
                                "", data_json, _now_utc()
                            ),
                        )
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=updated.fence_token,
                            call_number=updated.reserved_calls,
                            remaining_calls=0,
                            reason="tokens_exhausted",
                            ledger=updated,
                        )
                    if ledger.lease_expires_at > now and ledger.lease_owner_id != worker_id:
                        self._conn.execute("COMMIT")
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=max(0, max_calls - ledger.reserved_calls),
                            reason="active_lease_held",
                            ledger=ledger,
                        )

                    new_calls = ledger.reserved_calls + 1
                    new_fence = ledger.fence_token + 1
                    new_reserved = ledger.reserved_tokens + est_tokens
                    new_reserved_prompt = ledger.reserved_prompt_tokens + estimated_prompt_tokens
                    new_reserved_output = ledger.reserved_output_tokens + max_output_tokens
                    updated = KnowledgeGraphBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=new_calls,
                        completed_calls=ledger.completed_calls,
                        reserved_tokens=new_reserved,
                        spent_tokens=ledger.spent_tokens,
                        reserved_prompt_tokens=new_reserved_prompt,
                        reserved_output_tokens=new_reserved_output,
                        spent_prompt_tokens=ledger.spent_prompt_tokens,
                        spent_output_tokens=ledger.spent_output_tokens,
                        lease_owner_id=worker_id,
                        lease_expires_at=now + lease_duration_s,
                        fence_token=new_fence,
                        reconciled_fences=ledger.reconciled_fences,
                        status="active",
                        updated_at=_now_utc(),
                    )
                else:
                    new_calls = 1
                    new_fence = 1
                    updated = KnowledgeGraphBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=new_calls,
                        completed_calls=0,
                        reserved_tokens=est_tokens,
                        spent_tokens=0,
                        reserved_prompt_tokens=estimated_prompt_tokens,
                        reserved_output_tokens=max_output_tokens,
                        spent_prompt_tokens=0,
                        spent_output_tokens=0,
                        lease_owner_id=worker_id,
                        lease_expires_at=now + lease_duration_s,
                        fence_token=new_fence,
                        reconciled_fences=(),
                        status="active",
                        updated_at=_now_utc(),
                    )

                data_json = json.dumps(updated.model_dump())
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        updated.workspace_id, pk, sk, "", "", "", "",
                        "budget_ledger", updated.document_id, updated.document_version,
                        "", data_json, _now_utc()
                    ),
                )
                self._conn.execute("COMMIT")
                return KnowledgeGraphReservationResult(
                    granted=True,
                    fence_token=new_fence,
                    call_number=new_calls,
                    remaining_calls=max_calls - new_calls,
                    reason="granted",
                    ledger=updated,
                )
            except Exception as e:
                if in_tx:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                logger.error("SQLite reserve_extraction_budget error: %s", e)
                return KnowledgeGraphReservationResult(granted=False, reason="storage_error")

    def reconcile_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        actual_prompt_tokens: int,
        actual_output_tokens: int,
        status: str = "active",
    ) -> KnowledgeGraphReconciliationResult:
        worker_id = worker_id.strip() if worker_id else ""
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"

        with self._lock:
            in_tx = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                in_tx = True
            except sqlite3.OperationalError:
                return KnowledgeGraphReconciliationResult(reconciled=False, reason="storage_error")

            try:
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if not row:
                    self._conn.execute("COMMIT")
                    return KnowledgeGraphReconciliationResult(reconciled=False, reason="not_found")
                ledger = KnowledgeGraphBudgetLedger(**json.loads(row["data_json"]))

                if fence_token in ledger.reconciled_fences:
                    self._conn.execute("COMMIT")
                    return KnowledgeGraphReconciliationResult(reconciled=True, reason="already_reconciled", ledger=ledger)

                fenced_out = (ledger.fence_token != fence_token) or (ledger.lease_owner_id != worker_id)

                actual_total = actual_prompt_tokens + actual_output_tokens
                new_spent_prompt = ledger.spent_prompt_tokens + actual_prompt_tokens
                new_spent_output = ledger.spent_output_tokens + actual_output_tokens
                new_spent = ledger.spent_tokens + actual_total
                new_completed = ledger.completed_calls + 1
                new_rf = tuple(list(ledger.reconciled_fences) + [fence_token])

                if fenced_out:
                    updated = ledger.model_copy(update={
                        "spent_tokens": new_spent,
                        "spent_prompt_tokens": new_spent_prompt,
                        "spent_output_tokens": new_spent_output,
                        "completed_calls": new_completed,
                        "reconciled_fences": new_rf,
                        "updated_at": _now_utc(),
                    })
                    data_json = json.dumps(updated.model_dump())
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_knowledge_graph (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, document_id, document_version, publication_id,
                            data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            updated.workspace_id, pk, sk, "", "", "", "",
                            "budget_ledger", updated.document_id, updated.document_version,
                            "", data_json, _now_utc()
                        ),
                    )
                    self._conn.execute("COMMIT")
                    return KnowledgeGraphReconciliationResult(
                        reconciled=False,
                        fenced_out=True,
                        reason="fenced_out",
                        ledger=updated,
                    )

                updated = KnowledgeGraphBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=ledger.reserved_calls,
                    completed_calls=new_completed,
                    reserved_tokens=ledger.reserved_tokens,
                    spent_tokens=new_spent,
                    reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                    reserved_output_tokens=ledger.reserved_output_tokens,
                    spent_prompt_tokens=new_spent_prompt,
                    spent_output_tokens=new_spent_output,
                    lease_owner_id=worker_id,
                    lease_expires_at=ledger.lease_expires_at,
                    fence_token=ledger.fence_token,
                    reconciled_fences=new_rf,
                    status=status if status in ("active", "completed", "exhausted", "failed") else ledger.status,
                    updated_at=_now_utc(),
                )
                data_json = json.dumps(updated.model_dump())
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        updated.workspace_id, pk, sk, "", "", "", "",
                        "budget_ledger", updated.document_id, updated.document_version,
                        "", data_json, _now_utc()
                    ),
                )
                self._conn.execute("COMMIT")
                return KnowledgeGraphReconciliationResult(
                    reconciled=True,
                    fenced_out=False,
                    reason="reconciled",
                    ledger=updated,
                )
            except Exception as e:
                if in_tx:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                logger.error("SQLite reconcile_extraction_budget error: %s", e)
                return KnowledgeGraphReconciliationResult(reconciled=False, reason="storage_error")

    def fail_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        error_reason: str,
        prompt_tokens_spent: int = 0,
        output_tokens_spent: int = 0,
    ) -> None:
        worker_id = worker_id.strip() if worker_id else ""
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"

        with self._lock:
            in_tx = False
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                in_tx = True
            except sqlite3.OperationalError:
                return

            try:
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_knowledge_graph WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if not row:
                    self._conn.execute("COMMIT")
                    return
                ledger = KnowledgeGraphBudgetLedger(**json.loads(row["data_json"]))

                if fence_token in ledger.reconciled_fences:
                    if ledger.fence_token == fence_token and ledger.lease_owner_id == worker_id:
                        updated = ledger.model_copy(update={
                            "lease_owner_id": None,
                            "lease_expires_at": 0.0,
                            "updated_at": _now_utc(),
                        })
                        self._conn.execute(
                            "UPDATE kre_knowledge_graph SET data_json = ? WHERE pk = ? AND sk = ?",
                            (json.dumps(updated.model_dump()), pk, sk),
                        )
                        self._conn.execute("COMMIT")
                    else:
                        self._conn.execute("COMMIT")
                    return

                actual_total = prompt_tokens_spent + output_tokens_spent
                new_spent_prompt = ledger.spent_prompt_tokens + prompt_tokens_spent
                new_spent_output = ledger.spent_output_tokens + output_tokens_spent
                new_spent = ledger.spent_tokens + actual_total
                new_rf = tuple(list(ledger.reconciled_fences) + [fence_token])

                owner = ledger.lease_owner_id
                expires = ledger.lease_expires_at
                if ledger.fence_token == fence_token and owner == worker_id:
                    owner = None
                    expires = 0.0

                updated = ledger.model_copy(update={
                    "spent_tokens": new_spent,
                    "spent_prompt_tokens": new_spent_prompt,
                    "spent_output_tokens": new_spent_output,
                    "lease_owner_id": owner,
                    "lease_expires_at": expires,
                    "reconciled_fences": new_rf,
                    "updated_at": _now_utc(),
                })
                data_json = json.dumps(updated.model_dump())
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_knowledge_graph (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, document_id, document_version, publication_id,
                        data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        updated.workspace_id, pk, sk, "", "", "", "",
                        "budget_ledger", updated.document_id, updated.document_version,
                        "", data_json, _now_utc()
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception as e:
                if in_tx:
                    try:
                        self._conn.execute("ROLLBACK")
                    except Exception:
                        pass
                logger.error("SQLite fail_extraction_budget error: %s", e)


class DynamoDBKnowledgeGraphRepository:
    """Production DynamoDB storage implementation for partitioned Knowledge Graph records."""

    def __init__(self, table_name: str | None = None, endpoint_url: str | None = None) -> None:
        import boto3
        from aws.infra import get_resource

        self.table_name = table_name or getattr(settings, "DYNAMODB_GRAPH_TABLE_NAME", "kre_knowledge_graph")
        self.endpoint_url = endpoint_url
        if endpoint_url:
            # DynamoDB Local test convention: local emulators require static dummy credentials.
            # Live production environments leave endpoint_url as None to use IAM roles via ambient AWS credentials.
            self._dynamodb = boto3.resource(
                "dynamodb",
                endpoint_url=endpoint_url,
                region_name="us-east-1",
                aws_access_key_id="test",
                aws_secret_access_key="test",
            )
        else:
            self._dynamodb = get_resource("dynamodb")
        self._table = self._dynamodb.Table(self.table_name)
        disable_client_retries(self._table.meta.client)
        if hasattr(self._dynamodb, "meta") and hasattr(self._dynamodb.meta, "client"):
            disable_client_retries(self._dynamodb.meta.client)

    def stage_relationships(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str,
        entities: Sequence[KnowledgeGraphEntity],
        relationships: Sequence[KnowledgeGraphRelationship],
    ) -> None:
        now_str = _now_utc()
        items = []

        for rel in relationships:
            pk = f"KG#{workspace_id}#ENTITY#{rel.source_entity_id}"
            sk = f"EDGE#{rel.target_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
            gsi1_pk = f"KG#{workspace_id}#TARGET#{rel.target_entity_id}"
            gsi1_sk = f"EDGE#{rel.source_entity_id}#{rel.canonical_predicate}#DOC#{rel.source_document_id}#V#{rel.source_document_version}#PUB#{rel.publication_id}"
            gsi2_pk = f"KG#{workspace_id}#DOC#{rel.source_document_id}#V#{rel.source_document_version}"
            gsi2_sk = f"PUB#{rel.publication_id}#EDGE#{rel.source_entity_id}#{rel.target_entity_id}#{rel.canonical_predicate}"

            item = {
                "PK": pk,
                "SK": sk,
                "GSI1_PK": gsi1_pk,
                "GSI1_SK": gsi1_sk,
                "GSI2_PK": gsi2_pk,
                "GSI2_SK": gsi2_sk,
                "workspace_id": workspace_id,
                "record_type": "edge",
                "document_id": document_id,
                "document_version": document_version,
                "publication_id": publication_id,
                "data_json": json.dumps(_serialize_relationship(rel)),
                "created_at": now_str,
            }
            items.append(item)

        for ent in entities:
            lookup_pk = f"KG#{workspace_id}#LOOKUP"
            mentions = {normalize_mention(ent.name), normalize_mention(ent.canonical_name)}
            for alias in ent.aliases:
                mentions.add(normalize_mention(alias))

            for m in mentions:
                if not m:
                    continue
                esc_m = escape_delimiter(m)
                esc_eid = escape_delimiter(ent.entity_id)
                lookup_sk = f"MENTION#{esc_m}#ENT#{esc_eid}#DOC#{document_id}#V#{document_version}#PUB#{publication_id}"
                m_item = {
                    "PK": lookup_pk,
                    "SK": lookup_sk,
                    "workspace_id": workspace_id,
                    "record_type": "mention",
                    "document_id": document_id,
                    "document_version": document_version,
                    "publication_id": publication_id,
                    "entity_id": ent.entity_id,
                    "data_json": json.dumps(ent.model_dump()),
                    "created_at": now_str,
                }
                items.append(m_item)

        # Batch write items
        with self._table.batch_writer() as batch:
            for item in items:
                batch.put_item(Item=item)

    def commit_manifest(
        self,
        workspace_id: str,
        manifest: KnowledgeGraphManifest,
        base_publication_id: str | None = None,
        fence_token: int | None = None,
        worker_id: str | None = None,
    ) -> None:
        import botocore.exceptions

        pk = f"KG#{workspace_id}#DOC#{manifest.document_id}#V#{manifest.document_version}"
        sk = "MANIFEST"
        now_str = _now_utc()
        item = {
            "PK": pk,
            "SK": sk,
            "workspace_id": workspace_id,
            "record_type": "manifest",
            "document_id": manifest.document_id,
            "document_version": manifest.document_version,
            "publication_id": manifest.publication_id,
            "status": manifest.status,
            "data_json": json.dumps(manifest.model_dump()),
            "created_at": now_str,
        }

        # Handle condition expressions
        cond_expr = None
        expr_vals = {}
        expr_names = {}

        if base_publication_id is not None:
            cond_expr = "#pub = :base_pub"
            expr_names["#pub"] = "publication_id"
            expr_vals[":base_pub"] = base_publication_id
        else:
            # First publication or empty
            cond_expr = "attribute_not_exists(PK)"

        if fence_token is not None:
            if not worker_id or not worker_id.strip():
                raise FencingConflictError("Mandatory non-empty worker_id required for fenced publication")
            ledger_sk = "BUDGET#LEDGER"
            l_resp = self._table.get_item(
                Key={"PK": pk, "SK": ledger_sk},
                ConsistentRead=True,
            )
            l_item = l_resp.get("Item")
            if not l_item:
                raise FencingConflictError("Budget ledger does not exist for fenced publication")

            ld = json.loads(l_item["data_json"]) if "data_json" in l_item else l_item
            if ld.get("fence_token") != fence_token:
                raise FencingConflictError(f"Fence mismatch: expected {fence_token}, got {ld.get('fence_token')}")
            if ld.get("lease_owner_id") != worker_id:
                raise FencingConflictError(f"Lease owner mismatch: expected {worker_id}, got {ld.get('lease_owner_id')}")
            if float(ld.get("lease_expires_at", 0.0)) < time.time():
                raise FencingConflictError("Lease has expired for fenced publication")

            client = self._table.meta.client
            table_name = self._table.name
            now_ts = Decimal(str(time.time()))

            put_dict = {
                "TableName": table_name,
                "Item": item,
            }
            if cond_expr:
                put_dict["ConditionExpression"] = cond_expr
            if expr_names:
                put_dict["ExpressionAttributeNames"] = expr_names
            if expr_vals:
                put_dict["ExpressionAttributeValues"] = expr_vals

            update_ledger_dict = {
                "TableName": table_name,
                "Key": {
                    "PK": pk,
                    "SK": "BUDGET#LEDGER",
                },
                "UpdateExpression": "SET lease_owner_id = :none, lease_expires_at = :zero_dec, updated_at = :now_iso",
                "ConditionExpression": "attribute_exists(PK) AND #ft = :ft AND #owner = :owner AND #exp >= :now_ts",
                "ExpressionAttributeNames": {
                    "#ft": "fence_token",
                    "#owner": "lease_owner_id",
                    "#exp": "lease_expires_at",
                },
                "ExpressionAttributeValues": {
                    ":ft": fence_token,
                    ":owner": worker_id,
                    ":now_ts": now_ts,
                    ":none": None,
                    ":zero_dec": Decimal("0.0"),
                    ":now_iso": _now_utc(),
                },
            }

            try:
                client.transact_write_items(
                    TransactItems=[
                        {"Put": put_dict},
                        {"Update": update_ledger_dict},
                    ]
                )
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "TransactionCanceledException":
                    reasons = exc.response.get("CancellationReasons", [])
                    if len(reasons) > 1 and reasons[1].get("Code") == "ConditionalCheckFailed":
                        raise FencingConflictError(f"DynamoDB fenced authority validation failed: {reasons[1]}") from exc
                    raise OptimisticConcurrencyError(f"Publication conflict on {pk}: {reasons}") from exc
                raise
        else:
            try:
                if cond_expr:
                    kwargs = {
                        "Item": item,
                        "ConditionExpression": cond_expr,
                        "ExpressionAttributeValues": expr_vals if expr_vals else None,
                    }
                    if expr_names:
                        kwargs["ExpressionAttributeNames"] = expr_names
                    kwargs = {k: v for k, v in kwargs.items() if v is not None}
                    self._table.put_item(**kwargs)
                else:
                    self._table.put_item(Item=item)
            except botocore.exceptions.ClientError as exc:
                code = exc.response.get("Error", {}).get("Code")
                if code == "ConditionalCheckFailedException":
                    raise OptimisticConcurrencyError(
                        f"Publication conflict on {pk}: condition check failed"
                    ) from exc
                raise

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphManifest | None:
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "MANIFEST"
        resp = self._table.get_item(
            Key={"PK": pk, "SK": sk},
            ConsistentRead=consistent_read,
        )
        item = resp.get("Item")
        if not item:
            return None
        return KnowledgeGraphManifest(**json.loads(item["data_json"]))

    def resolve_mentions(
        self,
        workspace_id: str,
        candidate_tokens: Sequence[str],
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[dict[str, Any]], bool, str | None]:
        from boto3.dynamodb.conditions import Key

        lookup_pk = f"KG#{workspace_id}#LOOKUP"
        norm_tokens = [normalize_mention(t) for t in candidate_tokens if t]
        if not norm_tokens:
            return [], False, None

        results = []
        was_truncated = False
        truncation_reason = None
        pages_evaluated = 0

        seen_sks = set()
        for token in norm_tokens:
            esc_token = escape_delimiter(token)
            prefix = f"MENTION#{esc_token}"
            resp = self._table.query(
                KeyConditionExpression=Key("PK").eq(lookup_pk) & Key("SK").begins_with(prefix),
                Limit=limit,
            )
            pages_evaluated += 1
            items = resp.get("Items", [])
            for row in items:
                if row["SK"] in seen_sks:
                    continue
                seen_sks.add(row["SK"])
                data = json.loads(row["data_json"])
                results.append({
                    "SK": row["SK"],
                    "document_id": row.get("document_id"),
                    "document_version": row.get("document_version"),
                    "publication_id": row.get("publication_id"),
                    **data,
                })
                if len(results) >= limit:
                    break

            while "LastEvaluatedKey" in resp and len(results) < limit:
                if pages_evaluated >= max_pages:
                    was_truncated = True
                    truncation_reason = "mention_lookup_page_limit_reached"
                    break
                resp = self._table.query(
                    KeyConditionExpression=Key("PK").eq(lookup_pk) & Key("SK").begins_with(prefix),
                    ExclusiveStartKey=resp["LastEvaluatedKey"],
                    Limit=limit,
                )
                pages_evaluated += 1
                for row in resp.get("Items", []):
                    if row["SK"] in seen_sks:
                        continue
                    seen_sks.add(row["SK"])
                    data = json.loads(row["data_json"])
                    results.append({
                        "SK": row["SK"],
                        "document_id": row.get("document_id"),
                        "document_version": row.get("document_version"),
                        "publication_id": row.get("publication_id"),
                        **data,
                    })
                    if len(results) >= limit:
                        break

            if len(results) >= limit:
                break

        return results[:limit], was_truncated, truncation_reason

    def get_outgoing_edges(
        self,
        workspace_id: str,
        source_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        from boto3.dynamodb.conditions import Key

        pk = f"KG#{workspace_id}#ENTITY#{source_entity_id}"
        resp = self._table.query(
            KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("EDGE#"),
            Limit=limit,
        )
        pages_evaluated = 1
        items = resp.get("Items", [])
        was_truncated = False
        reason = None

        while "LastEvaluatedKey" in resp and len(items) < limit:
            if pages_evaluated >= max_pages:
                was_truncated = True
                reason = "edge_lookup_page_limit_reached"
                break
            resp = self._table.query(
                KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("EDGE#"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                Limit=limit,
            )
            pages_evaluated += 1
            items.extend(resp.get("Items", []))

        if not was_truncated:
            if "LastEvaluatedKey" in resp or len(items) > limit:
                was_truncated = True
                reason = "edge_lookup_page_limit_reached"

        edges = [_deserialize_relationship(json.loads(r["data_json"])) for r in items[:limit]]
        return edges, was_truncated, reason

    def get_incoming_edges(
        self,
        workspace_id: str,
        target_entity_id: str,
        limit: int = 10,
        max_pages: int = 3,
    ) -> tuple[list[KnowledgeGraphRelationship], bool, str | None]:
        from boto3.dynamodb.conditions import Key

        gsi1_pk = f"KG#{workspace_id}#TARGET#{target_entity_id}"
        resp = self._table.query(
            IndexName="gsi1_target_lookup",
            KeyConditionExpression=Key("GSI1_PK").eq(gsi1_pk) & Key("GSI1_SK").begins_with("EDGE#"),
            Limit=limit,
        )
        pages_evaluated = 1
        items = resp.get("Items", [])
        was_truncated = False
        reason = None

        while "LastEvaluatedKey" in resp and len(items) < limit:
            if pages_evaluated >= max_pages:
                was_truncated = True
                reason = "edge_lookup_page_limit_reached"
                break
            resp = self._table.query(
                IndexName="gsi1_target_lookup",
                KeyConditionExpression=Key("GSI1_PK").eq(gsi1_pk) & Key("GSI1_SK").begins_with("EDGE#"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                Limit=limit,
            )
            pages_evaluated += 1
            items.extend(resp.get("Items", []))

        if not was_truncated:
            if "LastEvaluatedKey" in resp or len(items) > limit:
                was_truncated = True
                reason = "edge_lookup_page_limit_reached"

        edges = [_deserialize_relationship(json.loads(r["data_json"])) for r in items[:limit]]
        return edges, was_truncated, reason

    def get_edges_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        publication_id: str | None = None,
    ) -> list[KnowledgeGraphRelationship]:
        from boto3.dynamodb.conditions import Key

        gsi2_pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        if publication_id:
            prefix = f"PUB#{publication_id}#"
        else:
            prefix = "PUB#"

        resp = self._table.query(
            IndexName="gsi2_doc_version",
            KeyConditionExpression=Key("GSI2_PK").eq(gsi2_pk) & Key("GSI2_SK").begins_with(prefix),
        )
        items = resp.get("Items", [])
        return [_deserialize_relationship(json.loads(r["data_json"])) for r in items]

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> KnowledgeGraphBudgetLedger | None:
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"
        resp = self._table.get_item(
            Key={"PK": pk, "SK": sk},
            ConsistentRead=consistent_read,
        )
        item = resp.get("Item")
        if not item:
            return None
        if "reserved_calls" in item:
            raw_rf = item.get("reconciled_fences", [])
            reconciled_fences = tuple(int(x) for x in raw_rf) if raw_rf else ()
            return KnowledgeGraphBudgetLedger(
                workspace_id=item["workspace_id"],
                document_id=item["document_id"],
                document_version=item["document_version"],
                reserved_calls=int(item.get("reserved_calls", 0)),
                completed_calls=int(item.get("completed_calls", 0)),
                reserved_tokens=int(item.get("reserved_tokens", 0)),
                spent_tokens=int(item.get("spent_tokens", 0)),
                reserved_prompt_tokens=int(item.get("reserved_prompt_tokens", 0)),
                reserved_output_tokens=int(item.get("reserved_output_tokens", 0)),
                spent_prompt_tokens=int(item.get("spent_prompt_tokens", 0)),
                spent_output_tokens=int(item.get("spent_output_tokens", 0)),
                lease_owner_id=item.get("lease_owner_id"),
                lease_expires_at=float(item.get("lease_expires_at", 0.0)),
                fence_token=int(item.get("fence_token", 0)),
                reconciled_fences=reconciled_fences,
                status=item.get("status", "active"),
                updated_at=item.get("updated_at", _now_utc()),
            )
        if "data_json" in item:
            return KnowledgeGraphBudgetLedger(**json.loads(item["data_json"]))
        return None

    def save_budget_ledger(
        self,
        ledger: KnowledgeGraphBudgetLedger,
    ) -> None:
        pk = f"KG#{ledger.workspace_id}#DOC#{ledger.document_id}#V#{ledger.document_version}"
        sk = "BUDGET#LEDGER"
        now_str = _now_utc()
        self._table.put_item(
            Item={
                "PK": pk,
                "SK": sk,
                "workspace_id": ledger.workspace_id,
                "record_type": "budget_ledger",
                "document_id": ledger.document_id,
                "document_version": ledger.document_version,
                "reserved_calls": ledger.reserved_calls,
                "completed_calls": ledger.completed_calls,
                "reserved_tokens": ledger.reserved_tokens,
                "spent_tokens": ledger.spent_tokens,
                "reserved_prompt_tokens": ledger.reserved_prompt_tokens,
                "reserved_output_tokens": ledger.reserved_output_tokens,
                "spent_prompt_tokens": ledger.spent_prompt_tokens,
                "spent_output_tokens": ledger.spent_output_tokens,
                "fence_token": ledger.fence_token,
                "lease_owner_id": ledger.lease_owner_id,
                "lease_expires_at": Decimal(str(ledger.lease_expires_at)),
                "reconciled_fences": list(ledger.reconciled_fences),
                "status": ledger.status,
                "data_json": json.dumps(ledger.model_dump()),
                "created_at": now_str,
                "updated_at": ledger.updated_at or now_str,
            }
        )

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 800,
        max_calls: int = 2,
        max_total_tokens: int = 5600,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1600,
    ) -> KnowledgeGraphReservationResult:
        if not worker_id or not worker_id.strip():
            return KnowledgeGraphReservationResult(granted=False, reason="storage_error")
        worker_id = worker_id.strip()

        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"
        now = time.time()
        est_tokens = estimated_prompt_tokens + max_output_tokens
        p_thresh = max_input_tokens - estimated_prompt_tokens
        o_thresh = max_output_ceiling - max_output_tokens
        t_thresh = max_total_tokens - est_tokens

        if max_calls < 1:
            curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
            return KnowledgeGraphReservationResult(granted=False, reason="calls_exhausted", ledger=curr)

        if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
            curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
            return KnowledgeGraphReservationResult(granted=False, reason="tokens_exhausted", ledger=curr)

        try:
            resp = self._table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=(
                    "SET #rc = if_not_exists(#rc, :zero) + :one, "
                    "#ft = if_not_exists(#ft, :zero) + :one, "
                    "#lown = :wid, "
                    "#lexp = :expires_at, "
                    "#st = :st_act, "
                    "#spent = if_not_exists(#spent, :zero), "
                    "#spt = if_not_exists(#spt, :zero), "
                    "#sot = if_not_exists(#sot, :zero), "
                    "#comp = if_not_exists(#comp, :zero), "
                    "#rt = if_not_exists(#rt, :zero) + :est_tokens, "
                    "#rpt = if_not_exists(#rpt, :zero) + :est_prompt, "
                    "#rot = if_not_exists(#rot, :zero) + :max_output, "
                    "workspace_id = :ws, "
                    "document_id = :did, "
                    "document_version = :ver, "
                    "record_type = :rtype, "
                    "updated_at = :now_iso"
                ),
                ConditionExpression=(
                    "("
                    "attribute_not_exists(PK) AND "
                    ":one <= :max_calls AND "
                    ":est_prompt <= :max_input AND "
                    ":max_output <= :max_out_ceiling AND "
                    ":est_tokens <= :max_total"
                    ") OR ("
                    "#rc < :max_calls AND "
                    "#st = :st_act AND "
                    "(attribute_not_exists(#rpt) OR #rpt <= :p_thresh) AND "
                    "(attribute_not_exists(#rot) OR #rot <= :o_thresh) AND "
                    "(attribute_not_exists(#rt) OR #rt <= :t_thresh) AND "
                    "(attribute_not_exists(#spt) OR #spt <= :p_thresh) AND "
                    "(attribute_not_exists(#sot) OR #sot <= :o_thresh) AND "
                    "(attribute_not_exists(#spent) OR #spent <= :t_thresh) AND "
                    "(#lexp < :now OR #lown = :wid)"
                    ")"
                ),
                ExpressionAttributeNames={
                    "#rc": "reserved_calls",
                    "#ft": "fence_token",
                    "#lown": "lease_owner_id",
                    "#lexp": "lease_expires_at",
                    "#st": "status",
                    "#spent": "spent_tokens",
                    "#spt": "spent_prompt_tokens",
                    "#sot": "spent_output_tokens",
                    "#comp": "completed_calls",
                    "#rt": "reserved_tokens",
                    "#rpt": "reserved_prompt_tokens",
                    "#rot": "reserved_output_tokens",
                },
                ExpressionAttributeValues={
                    ":zero": 0,
                    ":one": 1,
                    ":max_calls": max_calls,
                    ":max_input": max_input_tokens,
                    ":max_out_ceiling": max_output_ceiling,
                    ":max_total": max_total_tokens,
                    ":p_thresh": p_thresh,
                    ":o_thresh": o_thresh,
                    ":t_thresh": t_thresh,
                    ":est_tokens": est_tokens,
                    ":est_prompt": estimated_prompt_tokens,
                    ":max_output": max_output_tokens,
                    ":wid": worker_id,
                    ":expires_at": Decimal(str(round(now + lease_duration_s, 4))),
                    ":now": Decimal(str(round(now, 4))),
                    ":st_act": "active",
                    ":ws": workspace_id,
                    ":did": document_id,
                    ":ver": document_version,
                    ":rtype": "budget_ledger",
                    ":now_iso": _now_utc(),
                },
                ReturnValues="ALL_NEW",
            )
            attrs = resp.get("Attributes", {})
            raw_rf = attrs.get("reconciled_fences", [])
            reconciled_fences = tuple(int(x) for x in raw_rf) if raw_rf else ()
            ledger = KnowledgeGraphBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=int(attrs.get("reserved_calls", 1)),
                completed_calls=int(attrs.get("completed_calls", 0)),
                reserved_tokens=int(attrs.get("reserved_tokens", est_tokens)),
                spent_tokens=int(attrs.get("spent_tokens", 0)),
                reserved_prompt_tokens=int(attrs.get("reserved_prompt_tokens", estimated_prompt_tokens)),
                reserved_output_tokens=int(attrs.get("reserved_output_tokens", max_output_tokens)),
                spent_prompt_tokens=int(attrs.get("spent_prompt_tokens", 0)),
                spent_output_tokens=int(attrs.get("spent_output_tokens", 0)),
                lease_owner_id=attrs.get("lease_owner_id"),
                lease_expires_at=float(attrs.get("lease_expires_at", 0.0)),
                fence_token=int(attrs.get("fence_token", 1)),
                reconciled_fences=reconciled_fences,
                status=attrs.get("status", "active"),
                updated_at=attrs.get("updated_at", _now_utc()),
            )
            return KnowledgeGraphReservationResult(
                granted=True,
                fence_token=ledger.fence_token,
                call_number=ledger.reserved_calls,
                remaining_calls=max_calls - ledger.reserved_calls,
                reason="granted",
                ledger=ledger,
            )
        except Exception as exc:
            is_cond_err = False
            if hasattr(exc, "response") and isinstance(exc.response, dict):
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    is_cond_err = True
            elif "ConditionalCheckFailedException" in str(exc):
                is_cond_err = True

            if is_cond_err:
                curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                if curr is not None:
                    if curr.status in ("completed", "exhausted", "failed"):
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=curr.fence_token,
                            call_number=curr.reserved_calls,
                            remaining_calls=0,
                            reason=f"already_{curr.status}",  # type: ignore[arg-type]
                            ledger=curr,
                        )
                    if curr.reserved_calls >= max_calls:
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=curr.fence_token,
                            call_number=curr.reserved_calls,
                            remaining_calls=0,
                            reason="calls_exhausted",
                            ledger=curr,
                        )
                    if (
                        curr.reserved_prompt_tokens > p_thresh
                        or curr.reserved_output_tokens > o_thresh
                        or curr.reserved_tokens > t_thresh
                        or curr.spent_prompt_tokens > p_thresh
                        or curr.spent_output_tokens > o_thresh
                        or curr.spent_tokens > t_thresh
                    ):
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=curr.fence_token,
                            call_number=curr.reserved_calls,
                            remaining_calls=0,
                            reason="tokens_exhausted",
                            ledger=curr,
                        )
                    if curr.lease_expires_at > now and curr.lease_owner_id != worker_id:
                        return KnowledgeGraphReservationResult(
                            granted=False,
                            fence_token=curr.fence_token,
                            call_number=curr.reserved_calls,
                            remaining_calls=max(0, max_calls - curr.reserved_calls),
                            reason="active_lease_held",
                            ledger=curr,
                        )
                return KnowledgeGraphReservationResult(granted=False, reason="calls_exhausted", ledger=curr)

            logger.error("DynamoDB reserve_extraction_budget error: %s", exc)
            return KnowledgeGraphReservationResult(granted=False, reason="storage_error", ledger=None)

    def reconcile_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        actual_prompt_tokens: int,
        actual_output_tokens: int,
        status: str = "active",
    ) -> KnowledgeGraphReconciliationResult:
        import botocore.exceptions

        worker_id = worker_id.strip() if worker_id else ""
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"

        curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
        if curr is None:
            return KnowledgeGraphReconciliationResult(reconciled=False, reason="not_found")
        if fence_token in curr.reconciled_fences:
            return KnowledgeGraphReconciliationResult(reconciled=True, reason="already_reconciled", ledger=curr)

        actual_total = actual_prompt_tokens + actual_output_tokens
        fenced_out = (curr.fence_token != fence_token) or (curr.lease_owner_id != worker_id) or (float(curr.lease_expires_at) < time.time())

        try:
            if not fenced_out:
                new_status = status if status in ("active", "completed", "exhausted", "failed") else curr.status
                try:
                    self._table.update_item(
                        Key={"PK": pk, "SK": sk},
                        UpdateExpression=(
                            "SET spent_tokens = if_not_exists(spent_tokens, :zero) + :tot, "
                            "spent_prompt_tokens = if_not_exists(spent_prompt_tokens, :zero) + :spt, "
                            "spent_output_tokens = if_not_exists(spent_output_tokens, :zero) + :sot, "
                            "completed_calls = if_not_exists(completed_calls, :zero) + :one, "
                            "#rf = list_append(if_not_exists(#rf, :empty_list), :this_fence), "
                            "#st = :st, "
                            "updated_at = :now_iso"
                        ),
                        ConditionExpression=(
                            "attribute_exists(PK) AND fence_token = :exp_fence AND lease_owner_id = :wid "
                            "AND (attribute_not_exists(#rf) OR NOT contains(#rf, :exp_fence))"
                        ),
                        ExpressionAttributeNames={"#rf": "reconciled_fences", "#st": "status"},
                        ExpressionAttributeValues={
                            ":zero": 0,
                            ":tot": actual_total,
                            ":spt": actual_prompt_tokens,
                            ":sot": actual_output_tokens,
                            ":one": 1,
                            ":empty_list": [],
                            ":this_fence": [fence_token],
                            ":st": new_status,
                            ":exp_fence": fence_token,
                            ":wid": worker_id,
                            ":now_iso": _now_utc(),
                        },
                    )
                    updated = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                    return KnowledgeGraphReconciliationResult(
                        reconciled=True,
                        fenced_out=False,
                        reason="reconciled",
                        ledger=updated,
                    )
                except botocore.exceptions.ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    curr2 = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                    if curr2 and fence_token in curr2.reconciled_fences:
                        return KnowledgeGraphReconciliationResult(reconciled=True, reason="already_reconciled", ledger=curr2)
                    fenced_out = True

            # Late or fenced out worker: record spend without altering current lease authority
            try:
                self._table.update_item(
                    Key={"PK": pk, "SK": sk},
                    UpdateExpression=(
                        "SET spent_tokens = if_not_exists(spent_tokens, :zero) + :tot, "
                        "spent_prompt_tokens = if_not_exists(spent_prompt_tokens, :zero) + :spt, "
                        "spent_output_tokens = if_not_exists(spent_output_tokens, :zero) + :sot, "
                        "completed_calls = if_not_exists(completed_calls, :zero) + :one, "
                        "#rf = list_append(if_not_exists(#rf, :empty_list), :this_fence), "
                        "updated_at = :now_iso"
                    ),
                    ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#rf) OR NOT contains(#rf, :exp_fence))",
                    ExpressionAttributeNames={"#rf": "reconciled_fences"},
                    ExpressionAttributeValues={
                        ":zero": 0,
                        ":tot": actual_total,
                        ":spt": actual_prompt_tokens,
                        ":sot": actual_output_tokens,
                        ":one": 1,
                        ":empty_list": [],
                        ":this_fence": [fence_token],
                        ":exp_fence": fence_token,
                        ":now_iso": _now_utc(),
                    },
                )
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise

            updated = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
            return KnowledgeGraphReconciliationResult(
                reconciled=False,
                fenced_out=True,
                reason="fenced_out",
                ledger=updated,
            )
        except Exception as exc:
            logger.error("DynamoDB reconcile_extraction_budget error: %s", exc)
            return KnowledgeGraphReconciliationResult(reconciled=False, reason="storage_error")

    def fail_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        fence_token: int,
        error_reason: str,
        prompt_tokens_spent: int = 0,
        output_tokens_spent: int = 0,
    ) -> None:
        import botocore.exceptions

        worker_id = worker_id.strip() if worker_id else ""
        pk = f"KG#{workspace_id}#DOC#{document_id}#V#{document_version}"
        sk = "BUDGET#LEDGER"

        curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
        if curr is None:
            return
        if fence_token in curr.reconciled_fences:
            if curr.fence_token == fence_token and curr.lease_owner_id == worker_id:
                try:
                    self._table.update_item(
                        Key={"PK": pk, "SK": sk},
                        UpdateExpression="SET lease_owner_id = :none, lease_expires_at = :zero_dec, updated_at = :now_iso",
                        ConditionExpression="attribute_exists(PK) AND fence_token = :exp_fence AND lease_owner_id = :wid",
                        ExpressionAttributeValues={
                            ":none": None,
                            ":zero_dec": Decimal(0),
                            ":now_iso": _now_utc(),
                            ":exp_fence": fence_token,
                            ":wid": worker_id,
                        },
                    )
                except Exception:
                    pass
            return

        actual_total = prompt_tokens_spent + output_tokens_spent

        try:
            if curr.fence_token == fence_token and curr.lease_owner_id == worker_id:
                try:
                    self._table.update_item(
                        Key={"PK": pk, "SK": sk},
                        UpdateExpression=(
                            "SET spent_tokens = if_not_exists(spent_tokens, :zero) + :tot, "
                            "spent_prompt_tokens = if_not_exists(spent_prompt_tokens, :zero) + :spt, "
                            "spent_output_tokens = if_not_exists(spent_output_tokens, :zero) + :sot, "
                            "#rf = list_append(if_not_exists(#rf, :empty_list), :this_fence), "
                            "lease_owner_id = :none, "
                            "lease_expires_at = :zero_dec, "
                            "updated_at = :now_iso"
                        ),
                        ConditionExpression=(
                            "attribute_exists(PK) AND fence_token = :exp_fence AND lease_owner_id = :wid "
                            "AND (attribute_not_exists(#rf) OR NOT contains(#rf, :exp_fence))"
                        ),
                        ExpressionAttributeNames={"#rf": "reconciled_fences"},
                        ExpressionAttributeValues={
                            ":zero": 0,
                            ":zero_dec": Decimal("0.0"),
                            ":tot": actual_total,
                            ":spt": prompt_tokens_spent,
                            ":sot": output_tokens_spent,
                            ":empty_list": [],
                            ":this_fence": [fence_token],
                            ":none": None,
                            ":exp_fence": fence_token,
                            ":wid": worker_id,
                            ":now_iso": _now_utc(),
                        },
                    )
                    return
                except botocore.exceptions.ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    curr2 = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                    if curr2 and fence_token in curr2.reconciled_fences:
                        return

            # Fenced out / late worker: record spend without altering current lease authority
            try:
                self._table.update_item(
                    Key={"PK": pk, "SK": sk},
                    UpdateExpression=(
                        "SET spent_tokens = if_not_exists(spent_tokens, :zero) + :tot, "
                        "spent_prompt_tokens = if_not_exists(spent_prompt_tokens, :zero) + :spt, "
                        "spent_output_tokens = if_not_exists(spent_output_tokens, :zero) + :sot, "
                        "#rf = list_append(if_not_exists(#rf, :empty_list), :this_fence), "
                        "updated_at = :now_iso"
                    ),
                    ConditionExpression="attribute_exists(PK) AND (attribute_not_exists(#rf) OR NOT contains(#rf, :exp_fence))",
                    ExpressionAttributeNames={"#rf": "reconciled_fences"},
                    ExpressionAttributeValues={
                        ":zero": 0,
                        ":tot": actual_total,
                        ":spt": prompt_tokens_spent,
                        ":sot": output_tokens_spent,
                        ":empty_list": [],
                        ":this_fence": [fence_token],
                        ":exp_fence": fence_token,
                        ":now_iso": _now_utc(),
                    },
                )
            except botocore.exceptions.ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                    raise
        except Exception as exc:
            logger.error("DynamoDB fail_extraction_budget error: %s", exc)


_SHARED_KG_REPO: KnowledgeGraphRepositoryBase | None = None
_KG_REPO_LOCK = threading.Lock()


def get_knowledge_graph_repository(
    backend: str | None = None,
    db_path: str | Path | None = None,
    table_name: str | None = None,
) -> KnowledgeGraphRepositoryBase:
    """Factory creating KnowledgeGraphRepository instance according to environment or backend flag."""
    chosen = backend or getattr(settings, "GRAPH_REPO_BACKEND", None)

    if chosen == "memory" or os.environ.get("ENVIRONMENT") == "test":
        return InMemoryKnowledgeGraphRepository()
    elif chosen == "sqlite":
        return SQLiteKnowledgeGraphRepository(db_path=db_path)
    elif chosen == "dynamodb":
        return DynamoDBKnowledgeGraphRepository(table_name=table_name)
    else:
        # Default for local development
        return SQLiteKnowledgeGraphRepository(db_path=db_path)


def get_shared_knowledge_graph_repository(
    backend: str | None = None,
    db_path: str | Path | None = None,
    table_name: str | None = None,
    reset: bool = False,
) -> KnowledgeGraphRepositoryBase:
    """Resolve and return singleton Knowledge Graph repository matching configured environment."""
    global _SHARED_KG_REPO
    with _KG_REPO_LOCK:
        if reset:
            _SHARED_KG_REPO = None
            return None
        if _SHARED_KG_REPO is None:
            _SHARED_KG_REPO = get_knowledge_graph_repository(
                backend=backend, db_path=db_path, table_name=table_name
            )
        return _SHARED_KG_REPO


def build_pinned_snapshot_context(
    workspace_id: str,
    boundary_repo: Any,
    kg_repo: KnowledgeGraphRepositoryBase,
) -> PinnedSnapshotContext:
    """Acquire deeply immutable request-local snapshot context pinned at query start.
    
    Evaluates dynamic workspace tombstone. Invokes boundary_repo.list_document_snapshots.
    Queries kg_repo.get_manifest using strongly consistent reads.
    Evaluates dynamic source tombstones via boundary_repo.has_tombstone(workspace_id, "source", doc_id).
    Filters for manifests with status == "ready".
    Returns deeply immutable PinnedSnapshotContext.
    """
    now_utc = _now_utc()
    if boundary_repo and hasattr(boundary_repo, "has_tombstone"):
        if boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
            return PinnedSnapshotContext(
                workspace_id=workspace_id,
                captured_at_utc=now_utc,
                active_documents=(),
            )

    snapshots = []
    if boundary_repo and hasattr(boundary_repo, "list_document_snapshots"):
        try:
            snapshots = boundary_repo.list_document_snapshots(workspace_id)
        except Exception as e:
            logger.warning("Failed to list document snapshots for workspace %s: %s", workspace_id, e)
            snapshots = []

    pinned_docs: list[PinnedDocumentVersion] = []
    for s in snapshots:
        did = getattr(s, "document_id", None) or (s.get("document_id") if isinstance(s, dict) else None)
        ver = getattr(s, "version", None) or (s.get("version") if isinstance(s, dict) else None)
        if not did or not ver:
            continue

        is_tomb = False
        if boundary_repo and hasattr(boundary_repo, "has_tombstone"):
            is_tomb = boundary_repo.has_tombstone(workspace_id, "source", str(did))

        manifest = kg_repo.get_manifest(workspace_id, str(did), str(ver), consistent_read=True)
        if manifest is not None and manifest.status == "ready":
            pinned_docs.append(
                PinnedDocumentVersion(
                    document_id=str(did),
                    document_version=str(ver),
                    publication_id=manifest.publication_id,
                    is_tombstoned=is_tomb,
                )
            )

    return PinnedSnapshotContext(
        workspace_id=workspace_id,
        captured_at_utc=now_utc,
        active_documents=tuple(pinned_docs),
    )


import sys
if __name__ in ("db.knowledge_graph_repo", "src.db.knowledge_graph_repo"):
    sys.modules["db.knowledge_graph_repo"] = sys.modules[__name__]
    sys.modules["src.db.knowledge_graph_repo"] = sys.modules[__name__]
