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
        KnowledgeGraphRelationship,
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
        KnowledgeGraphRelationship,
        PinnedDocumentVersion,
        PinnedSnapshotContext,
        canonicalize_predicate,
        escape_delimiter,
        normalize_mention,
        unescape_delimiter,
    )
    from schemas.contracts.location import DocumentLocation, TableLocation

logger = logging.getLogger(__name__)


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
            page_capacity = 10
            max_eval = page_capacity * max_pages  # 30 items
            was_truncated = False
            truncation_reason = None

            norm_tokens = {normalize_mention(t) for t in candidate_tokens if t}

            for item in sorted(candidates, key=lambda x: x["SK"]):
                evaluated_count += 1
                if evaluated_count > max_eval:
                    was_truncated = True
                    truncation_reason = "mention_lookup_page_limit_reached"
                    break

                sk = item["SK"]  # MENTION#{token}#ENT#{ent_id}...
                parts = sk.split("#")
                if len(parts) >= 3 and parts[0] == "MENTION":
                    token = unescape_delimiter(parts[1])
                    if token in norm_tokens or any(token.startswith(nt) for nt in norm_tokens):
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

            max_eval = 10 * max_pages  # 30 items
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

            max_eval = 10 * max_pages
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


class SQLiteKnowledgeGraphRepository:
    """Durable SQLite storage with WAL mode for Knowledge Graph enrichment."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            configured = getattr(settings, "GRAPH_SQLITE_PATH", None) or getattr(settings, "WORKSPACE_SQLITE_PATH", None)
            if configured:
                self.db_path = Path(configured)
            else:
                self.db_path = Path(__file__).resolve().parent.parent.parent / "data" / "kre_knowledge_graph.db"
        else:
            self.db_path = Path(db_path)

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
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
            with self._conn:
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

        max_eval = 10 * max_pages  # 30 items
        results: list[dict[str, Any]] = []
        was_truncated = False
        truncation_reason = None

        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT sk, document_id, document_version, publication_id, data_json FROM kre_knowledge_graph
                WHERE pk = ? AND sk LIKE 'MENTION#%'
                ORDER BY sk ASC
                LIMIT ?
                """,
                (lookup_pk, max_eval + 1),
            )
            rows = cursor.fetchall()
            if len(rows) > max_eval:
                was_truncated = True
                truncation_reason = "mention_lookup_page_limit_reached"
                rows = rows[:max_eval]

            for row in rows:
                sk = row["sk"]
                parts = sk.split("#")
                if len(parts) >= 3:
                    token = unescape_delimiter(parts[1])
                    if any(token == nt or token.startswith(nt) for nt in norm_tokens):
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


class DynamoDBKnowledgeGraphRepository:
    """Production DynamoDB storage implementation for partitioned Knowledge Graph records."""

    def __init__(self, table_name: str | None = None, endpoint_url: str | None = None) -> None:
        import boto3
        from aws.infra import get_resource

        self.table_name = table_name or getattr(settings, "DYNAMODB_GRAPH_TABLE_NAME", "kre_knowledge_graph")
        self.endpoint_url = endpoint_url
        if endpoint_url:
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

        while "LastEvaluatedKey" in resp and len(items) < limit:
            if pages_evaluated >= max_pages:
                return (
                    [_deserialize_relationship(json.loads(r["data_json"])) for r in items[:limit]],
                    True,
                    "edge_lookup_page_limit_reached",
                )
            resp = self._table.query(
                KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("EDGE#"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                Limit=limit,
            )
            pages_evaluated += 1
            items.extend(resp.get("Items", []))

        was_truncated = "LastEvaluatedKey" in resp
        reason = "edge_lookup_page_limit_reached" if was_truncated else None
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

        while "LastEvaluatedKey" in resp and len(items) < limit:
            if pages_evaluated >= max_pages:
                return (
                    [_deserialize_relationship(json.loads(r["data_json"])) for r in items[:limit]],
                    True,
                    "edge_lookup_page_limit_reached",
                )
            resp = self._table.query(
                IndexName="gsi1_target_lookup",
                KeyConditionExpression=Key("GSI1_PK").eq(gsi1_pk) & Key("GSI1_SK").begins_with("EDGE#"),
                ExclusiveStartKey=resp["LastEvaluatedKey"],
                Limit=limit,
            )
            pages_evaluated += 1
            items.extend(resp.get("Items", []))

        was_truncated = "LastEvaluatedKey" in resp
        reason = "edge_lookup_page_limit_reached" if was_truncated else None
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
        return KnowledgeGraphBudgetLedger(**json.loads(item["data_json"]))

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
                "data_json": json.dumps(ledger.model_dump()),
                "created_at": now_str,
            }
        )


_DEFAULT_KG_REPO: KnowledgeGraphRepositoryBase | None = None


def get_knowledge_graph_repository(
    backend: str | None = None,
    db_path: str | Path | None = None,
    table_name: str | None = None,
) -> KnowledgeGraphRepositoryBase:
    """Factory creating KnowledgeGraphRepository instance according to environment or backend flag."""
    global _DEFAULT_KG_REPO
    chosen = backend or getattr(settings, "GRAPH_REPO_BACKEND", None)

    if chosen == "memory" or os.environ.get("ENVIRONMENT") == "test":
        if _DEFAULT_KG_REPO is None or not isinstance(_DEFAULT_KG_REPO, InMemoryKnowledgeGraphRepository):
            _DEFAULT_KG_REPO = InMemoryKnowledgeGraphRepository()
        return _DEFAULT_KG_REPO
    elif chosen == "sqlite":
        return SQLiteKnowledgeGraphRepository(db_path=db_path)
    elif chosen == "dynamodb":
        return DynamoDBKnowledgeGraphRepository(table_name=table_name)
    else:
        # Default for local development
        return SQLiteKnowledgeGraphRepository(db_path=db_path)


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
