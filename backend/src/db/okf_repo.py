"""Partitioned repository for Open Knowledge Framework (OKF) facts.

Provides workspace-partitioned storage for OkfEntityRecord, OkfPropertyAssertion,
and OkfManifest with GSI1 (entity resolution) and GSI2 (document readiness and scoping).
Supports in-memory test storage, durable SQLite persistence, and DynamoDB.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Protocol, Sequence, runtime_checkable

from config import settings
from src.schemas.contracts.boundary import StorageUnavailableError
from src.schemas.contracts.okf import (
    OkfEntityRecord,
    OkfManifest,
    OkfPropertyAssertion,
    normalize_entity_name,
    normalize_property_key,
)

logger = logging.getLogger(__name__)


def create_dedicated_storage_config(connect_timeout: float = 0.030, read_timeout: float = 0.040):
    """Create botocore Config with hard socket timeouts and zero SDK retries."""
    from botocore.config import Config
    return Config(
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
        retries={"max_attempts": 1, "mode": "standard"},
    )


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
                try:
                    events.unregister("needs-retry", unique_id=uid)
                except Exception:
                    pass
    except Exception as exc:
        logger.debug("Failed to unregister retry handler: %s", exc)


@runtime_checkable
class OkfRepositoryBase(Protocol):
    """Authoritative repository operations for OKF entities and assertions."""

    def save_manifest(self, workspace_id: str, manifest: OkfManifest) -> None:
        """Atomically persist manifest entities and validated assertions under workspace PK."""
        ...

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
    ) -> list[OkfEntityRecord]:
        """Query GSI1 by normalized name prefix or exact match with bounded limit. Avoids scans."""
        ...

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[OkfPropertyAssertion]:
        """Query Main table by PK=WORKSPACE#{ws_id} and SK begins_with ENTITY#{entity_id}."""
        ...

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> bool:
        """Query GSI2 with Limit=1 to verify whether active OKF assertions exist."""
        ...

    def list_assertions_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[OkfPropertyAssertion], str | None]:
        """Paginated query over GSI2 for document assertions with bounded limit."""
        ...

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> OkfManifest | None:
        """Retrieve manifest for a given workspace and document version."""
        ...

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        """Delete all entities and assertions for a document or document version."""
        ...

    def reset_fixtures(self) -> None:
        """Reset all fixtures in memory / storage."""
        ...


def _anchor_entity_to_manifest(
    ent: OkfEntityRecord,
    workspace_id: str,
    document_id: str,
    document_version: str,
) -> OkfEntityRecord:
    if ent.document_id != document_id or ent.workspace_id != workspace_id or ent.document_version != document_version:
        return OkfEntityRecord(
            workspace_id=workspace_id,
            entity_id=ent.entity_id,
            entity_name=ent.entity_name,
            normalized_name=ent.normalized_name,
            entity_type=ent.entity_type,
            document_id=document_id,
            document_version=document_version,
            created_at=ent.created_at,
        )
    return ent


def _anchor_assertion_to_manifest(
    ass: OkfPropertyAssertion,
    workspace_id: str,
    document_id: str,
    document_version: str,
) -> OkfPropertyAssertion:
    if ass.document_id != document_id or ass.workspace_id != workspace_id or ass.document_version != document_version:
        return OkfPropertyAssertion(
            assertion_id=ass.assertion_id,
            workspace_id=workspace_id,
            entity_id=ass.entity_id,
            entity_name=ass.entity_name,
            property_key=ass.property_key,
            property_value=ass.property_value,
            value_type=ass.value_type,
            source_chunk_id=ass.source_chunk_id,
            document_id=document_id,
            document_version=document_version,
            page_number=ass.page_number,
            table_name=ass.table_name,
            column_name=ass.column_name,
            confidence_score=ass.confidence_score,
            validation_status=ass.validation_status,
            created_at=ass.created_at,
        )
    return ass


class InMemoryOkfRepository:
    """Thread safe in memory OKF repository strictly for isolated test suites."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[tuple[str, str, str, str], OkfEntityRecord] = {}  # (ws, entity_id, doc_id, ver)
        self._assertions: dict[tuple[str, str, str], OkfPropertyAssertion] = {}  # (ws, entity_id, assertion_id)
        self._manifests: dict[tuple[str, str, str], OkfManifest] = {}  # (ws, doc_id, ver)

    def reset_fixtures(self) -> None:
        with self._lock:
            self._entities.clear()
            self._assertions.clear()
            self._manifests.clear()

    def save_manifest(self, workspace_id: str, manifest: OkfManifest) -> None:
        with self._lock:
            # Save manifest
            self._manifests[(workspace_id, manifest.document_id, manifest.document_version)] = manifest
            # Save entities anchored to manifest scope
            for ent in manifest.entities:
                ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version)
                self._entities[(workspace_id, ent.entity_id, ent.document_id, ent.document_version)] = ent
            # Save assertions anchored to manifest scope
            for ass in manifest.assertions:
                ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version)
                self._assertions[(workspace_id, ass.entity_id, ass.assertion_id)] = ass

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
    ) -> list[OkfEntityRecord]:
        with self._lock:
            norm_candidates = {normalize_entity_name(name) for name in candidate_names if name}
            if not norm_candidates:
                return []

            matched: list[OkfEntityRecord] = []
            for (ws, _, _, _), ent in self._entities.items():
                if ws != workspace_id:
                    continue
                ent_norm = ent.normalized_name
                # Check exact or prefix match against any candidate
                for cand in norm_candidates:
                    if ent_norm == cand or ent_norm.startswith(cand):
                        matched.append(ent)
                        break
                if len(matched) >= limit:
                    break
            return matched[:limit]

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[OkfPropertyAssertion]:
        with self._lock:
            active_docs = set(active_document_ids) if active_document_ids is not None else None
            norm_key = normalize_property_key(property_key) if property_key else None

            matched: list[OkfPropertyAssertion] = []
            for (ws, ent_id, _), ass in self._assertions.items():
                if ws != workspace_id or ent_id != entity_id:
                    continue
                if active_docs is not None and ass.document_id not in active_docs:
                    continue
                if norm_key is not None and normalize_property_key(ass.property_key) != norm_key:
                    continue
                matched.append(ass)
                if len(matched) >= limit:
                    break
            return matched[:limit]

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> bool:
        with self._lock:
            man = self._manifests.get((workspace_id, document_id, document_version))
            if man and man.status == "ready":
                return True
            for (ws, _, _), ass in self._assertions.items():
                if ws == workspace_id and ass.document_id == document_id and ass.document_version == document_version:
                    return True
            return False

    def list_assertions_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[OkfPropertyAssertion], str | None]:
        with self._lock:
            matched = [
                ass for (ws, _, _), ass in self._assertions.items()
                if ws == workspace_id and ass.document_id == document_id and ass.document_version == document_version
            ]
            offset = int(next_token) if next_token and next_token.isdigit() else 0
            sliced = matched[offset : offset + limit]
            new_offset = offset + len(sliced)
            token = str(new_offset) if new_offset < len(matched) else None
            return sliced, token

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> OkfManifest | None:
        with self._lock:
            return self._manifests.get((workspace_id, document_id, document_version))

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        with self._lock:
            self._manifests = {
                k: v for k, v in self._manifests.items()
                if not (k[0] == workspace_id and k[1] == document_id and (document_version is None or k[2] == document_version))
            }
            self._entities = {
                k: v for k, v in self._entities.items()
                if not (k[0] == workspace_id and k[2] == document_id and (document_version is None or k[3] == document_version))
            }
            self._assertions = {
                k: v for k, v in self._assertions.items()
                if not (k[0] == workspace_id and v.document_id == document_id and (document_version is None or v.document_version == document_version))
            }


class SQLiteOkfRepository:
    """Durable SQLite implementation for local dev and testing."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            configured = getattr(settings, "OKF_SQLITE_PATH", None) or getattr(settings, "WORKSPACE_SQLITE_PATH", None)
            if configured:
                self.db_path = Path(configured)
            else:
                self.db_path = Path(__file__).resolve().parent.parent.parent / "data" / "kre_okf.db"
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
                    CREATE TABLE IF NOT EXISTS kre_okf_facts (
                        workspace_id TEXT NOT NULL,
                        pk TEXT NOT NULL,
                        sk TEXT NOT NULL,
                        gsi1_pk TEXT NOT NULL,
                        gsi1_sk TEXT NOT NULL,
                        gsi2_pk TEXT NOT NULL,
                        gsi2_sk TEXT NOT NULL,
                        record_type TEXT NOT NULL,
                        entity_id TEXT,
                        document_id TEXT NOT NULL,
                        document_version TEXT NOT NULL,
                        normalized_name TEXT,
                        property_key TEXT,
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (pk, sk)
                    )
                    """
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_okf_gsi1 ON kre_okf_facts (gsi1_pk, gsi1_sk)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_okf_gsi2 ON kre_okf_facts (gsi2_pk, gsi2_sk)"
                )
                self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_okf_entity ON kre_okf_facts (workspace_id, entity_id)"
                )

    def reset_fixtures(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM kre_okf_facts")

    def save_manifest(self, workspace_id: str, manifest: OkfManifest) -> None:
        with self._lock:
            with self._conn:
                # 1. Save manifest record
                man_pk = f"WORKSPACE#{workspace_id}"
                man_sk = f"MANIFEST#DOC#{manifest.document_id}#VER#{manifest.document_version}"
                gsi1_pk = man_pk
                gsi1_sk = f"MANIFEST#{manifest.document_id}"
                gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{manifest.document_id}"
                gsi2_sk = f"VER#{manifest.document_version}#MANIFEST"
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_okf_facts (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, entity_id, document_id, document_version,
                        normalized_name, property_key, data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id, man_pk, man_sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        "MANIFEST", None, manifest.document_id, manifest.document_version,
                        None, None, manifest.model_dump_json(), manifest.created_at,
                    ),
                )

                # 2. Save entity records
                for ent in manifest.entities:
                    ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version)
                    ent_pk = f"WORKSPACE#{workspace_id}"
                    ent_sk = f"ENTITY#{ent.entity_id}#DOC#{ent.document_id}#VER#{ent.document_version}"
                    ent_gsi1_pk = ent_pk
                    ent_gsi1_sk = f"ENTITY_NAME#{ent.normalized_name}"
                    ent_gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{ent.document_id}"
                    ent_gsi2_sk = f"VER#{ent.document_version}#ENTITY#{ent.entity_id}"
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id, ent_pk, ent_sk, ent_gsi1_pk, ent_gsi1_sk, ent_gsi2_pk, ent_gsi2_sk,
                            "ENTITY", ent.entity_id, ent.document_id, ent.document_version,
                            ent.normalized_name, None, ent.model_dump_json(), ent.created_at,
                        ),
                    )

                # 3. Save assertion records
                for ass in manifest.assertions:
                    ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version)
                    ass_pk = f"WORKSPACE#{workspace_id}"
                    ass_sk = f"ENTITY#{ass.entity_id}#ASSERTION#{ass.assertion_id}"
                    ass_gsi1_pk = ass_pk
                    ass_gsi1_sk = f"ASSERTION#{ass.assertion_id}"
                    ass_gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{ass.document_id}"
                    ass_gsi2_sk = f"VER#{ass.document_version}#ASSERTION#{ass.assertion_id}"
                    norm_k = normalize_property_key(ass.property_key)
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id, ass_pk, ass_sk, ass_gsi1_pk, ass_gsi1_sk, ass_gsi2_pk, ass_gsi2_sk,
                            "ASSERTION", ass.entity_id, ass.document_id, ass.document_version,
                            None, norm_k, ass.model_dump_json(), ass.created_at,
                        ),
                    )

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
    ) -> list[OkfEntityRecord]:
        with self._lock:
            norm_candidates = [normalize_entity_name(name) for name in candidate_names if name]
            if not norm_candidates:
                return []

            gsi1_pk = f"WORKSPACE#{workspace_id}"
            results: list[OkfEntityRecord] = []
            seen_entity_ids: set[str] = set()

            for cand in norm_candidates:
                # Query GSI1 by normalized name prefix or exact match
                cursor = self._conn.execute(
                    """
                    SELECT data_json FROM kre_okf_facts
                    WHERE gsi1_pk = ? AND record_type = 'ENTITY' AND gsi1_sk LIKE ?
                    LIMIT ?
                    """,
                    (gsi1_pk, f"ENTITY_NAME#{cand}%", limit),
                )
                rows = cursor.fetchall()
                for row in rows:
                    data = json.loads(row["data_json"])
                    rec = OkfEntityRecord(**data)
                    if rec.entity_id not in seen_entity_ids:
                        seen_entity_ids.add(rec.entity_id)
                        results.append(rec)
                        if len(results) >= limit:
                            return results

            return results

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[OkfPropertyAssertion]:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            sk_prefix = f"ENTITY#{entity_id}#ASSERTION#"

            query = (
                "SELECT data_json FROM kre_okf_facts "
                "WHERE pk = ? AND sk LIKE ? AND record_type = 'ASSERTION'"
            )
            params: list[Any] = [pk, f"{sk_prefix}%"]

            if property_key:
                norm_key = normalize_property_key(property_key)
                query += " AND property_key = ?"
                params.append(norm_key)

            if active_document_ids is not None:
                if not active_document_ids:
                    return []
                placeholders = ",".join("?" for _ in active_document_ids)
                query += f" AND document_id IN ({placeholders})"
                params.extend(active_document_ids)

            query += " LIMIT ?"
            params.append(limit)

            cursor = self._conn.execute(query, params)
            rows = cursor.fetchall()
            return [OkfPropertyAssertion(**json.loads(r["data_json"])) for r in rows]

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> bool:
        with self._lock:
            gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{document_id}"
            gsi2_prefix = f"VER#{document_version}#"

            cursor = self._conn.execute(
                """
                SELECT 1 FROM kre_okf_facts
                WHERE gsi2_pk = ? AND gsi2_sk LIKE ?
                LIMIT 1
                """,
                (gsi2_pk, f"{gsi2_prefix}%"),
            )
            return cursor.fetchone() is not None

    def list_assertions_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[OkfPropertyAssertion], str | None]:
        with self._lock:
            gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{document_id}"
            gsi2_prefix = f"VER#{document_version}#ASSERTION#"
            offset = int(next_token) if next_token and next_token.isdigit() else 0

            cursor = self._conn.execute(
                """
                SELECT data_json FROM kre_okf_facts
                WHERE gsi2_pk = ? AND gsi2_sk LIKE ? AND record_type = 'ASSERTION'
                LIMIT ? OFFSET ?
                """,
                (gsi2_pk, f"{gsi2_prefix}%", limit, offset),
            )
            rows = cursor.fetchall()
            assertions = [OkfPropertyAssertion(**json.loads(r["data_json"])) for r in rows]

            # Check if there are more items
            cursor_more = self._conn.execute(
                """
                SELECT 1 FROM kre_okf_facts
                WHERE gsi2_pk = ? AND gsi2_sk LIKE ? AND record_type = 'ASSERTION'
                LIMIT 1 OFFSET ?
                """,
                (gsi2_pk, f"{gsi2_prefix}%", offset + len(assertions)),
            )
            has_more = cursor_more.fetchone() is not None
            next_t = str(offset + len(assertions)) if has_more else None
            return assertions, next_t

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> OkfManifest | None:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            sk = f"MANIFEST#DOC#{document_id}#VER#{document_version}"
            cursor = self._conn.execute(
                "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                (pk, sk),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return OkfManifest(**json.loads(row["data_json"]))

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        with self._lock:
            with self._conn:
                if document_version is not None:
                    self._conn.execute(
                        "DELETE FROM kre_okf_facts WHERE workspace_id = ? AND document_id = ? AND document_version = ?",
                        (workspace_id, document_id, document_version),
                    )
                else:
                    self._conn.execute(
                        "DELETE FROM kre_okf_facts WHERE workspace_id = ? AND document_id = ?",
                        (workspace_id, document_id),
                    )


class DynamoDBOkfRepository:
    """Authoritative production OKF repository backed by Amazon DynamoDB."""

    def __init__(
        self,
        table_name: str | None = None,
        connect_timeout: float = 0.030,
        read_timeout: float = 0.040,
    ) -> None:
        self.table_name = table_name or getattr(settings, "DYNAMODB_OKF_TABLE_NAME", "kre_okf_facts")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._table: Any = None
        self._client: Any = None

    def _get_resources(self) -> tuple[Any, Any]:
        if self._table is not None and self._client is not None:
            return self._client, self._table

        import boto3
        from aws.infra import get_client

        cfg = create_dedicated_storage_config(self._connect_timeout, self._read_timeout)
        try:
            client = get_client("dynamodb", config=cfg)
        except Exception:
            session = boto3.Session()
            client = session.client(
                "dynamodb",
                region_name=getattr(settings, "AWS_REGION", "us-east-1"),
                config=cfg,
            )
        disable_client_retries(client)

        session = boto3.Session()
        resource = session.resource(
            "dynamodb",
            region_name=getattr(settings, "AWS_REGION", "us-east-1"),
            config=cfg,
        )
        table = resource.Table(self.table_name)

        disable_client_retries(client)
        if hasattr(resource, "meta") and hasattr(resource.meta, "client"):
            disable_client_retries(resource.meta.client)
        if hasattr(table, "meta") and hasattr(table.meta, "client"):
            disable_client_retries(table.meta.client)

        self._client = client
        self._table = table
        return self._client, self._table

    def reset_fixtures(self) -> None:
        # No-op in production DynamoDB table
        pass

    def save_manifest(self, workspace_id: str, manifest: OkfManifest) -> None:
        _, table = self._get_resources()
        try:
            with table.batch_writer() as batch:
                # Manifest item
                batch.put_item(
                    Item={
                        "PK": f"WORKSPACE#{workspace_id}",
                        "SK": f"MANIFEST#DOC#{manifest.document_id}#VER#{manifest.document_version}",
                        "GSI1PK": f"WORKSPACE#{workspace_id}",
                        "GSI1SK": f"MANIFEST#{manifest.document_id}",
                        "GSI2PK": f"WORKSPACE#{workspace_id}#DOC#{manifest.document_id}",
                        "GSI2SK": f"VER#{manifest.document_version}#MANIFEST",
                        "record_type": "MANIFEST",
                        "workspace_id": workspace_id,
                        "document_id": manifest.document_id,
                        "document_version": manifest.document_version,
                        "data_json": manifest.model_dump_json(),
                        "created_at": manifest.created_at,
                    }
                )

                # Entities
                for ent in manifest.entities:
                    ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version)
                    batch.put_item(
                        Item={
                            "PK": f"WORKSPACE#{workspace_id}",
                            "SK": f"ENTITY#{ent.entity_id}#DOC#{ent.document_id}#VER#{ent.document_version}",
                            "GSI1PK": f"WORKSPACE#{workspace_id}",
                            "GSI1SK": f"ENTITY_NAME#{ent.normalized_name}",
                            "GSI2PK": f"WORKSPACE#{workspace_id}#DOC#{ent.document_id}",
                            "GSI2SK": f"VER#{ent.document_version}#ENTITY#{ent.entity_id}",
                            "record_type": "ENTITY",
                            "workspace_id": workspace_id,
                            "entity_id": ent.entity_id,
                            "document_id": ent.document_id,
                            "document_version": ent.document_version,
                            "normalized_name": ent.normalized_name,
                            "data_json": ent.model_dump_json(),
                            "created_at": ent.created_at,
                        }
                    )

                # Assertions
                for ass in manifest.assertions:
                    ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version)
                    batch.put_item(
                        Item={
                            "PK": f"WORKSPACE#{workspace_id}",
                            "SK": f"ENTITY#{ass.entity_id}#ASSERTION#{ass.assertion_id}",
                            "GSI1PK": f"WORKSPACE#{workspace_id}",
                            "GSI1SK": f"ASSERTION#{ass.assertion_id}",
                            "GSI2PK": f"WORKSPACE#{workspace_id}#DOC#{ass.document_id}",
                            "GSI2SK": f"VER#{ass.document_version}#ASSERTION#{ass.assertion_id}",
                            "record_type": "ASSERTION",
                            "workspace_id": workspace_id,
                            "entity_id": ass.entity_id,
                            "document_id": ass.document_id,
                            "document_version": ass.document_version,
                            "property_key": normalize_property_key(ass.property_key),
                            "data_json": ass.model_dump_json(),
                            "created_at": ass.created_at,
                        }
                    )
        except Exception as exc:
            logger.error("DynamoDB save_manifest failed for doc %s: %s", manifest.document_id, exc)
            raise StorageUnavailableError(f"DynamoDB error saving OKF manifest: {exc}") from exc

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
    ) -> list[OkfEntityRecord]:
        _, table = self._get_resources()
        norm_candidates = [normalize_entity_name(name) for name in candidate_names if name]
        if not norm_candidates:
            return []

        from boto3.dynamodb.conditions import Key

        results: list[OkfEntityRecord] = []
        seen_entity_ids: set[str] = set()

        for cand in norm_candidates:
            try:
                resp = table.query(
                    IndexName="GSI1",
                    KeyConditionExpression=Key("GSI1PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("GSI1SK").begins_with(f"ENTITY_NAME#{cand}"),
                    Limit=limit,
                )
                items = resp.get("Items", [])
                for it in items:
                    if it.get("record_type") == "ENTITY":
                        rec = OkfEntityRecord(**json.loads(it["data_json"]))
                        if rec.entity_id not in seen_entity_ids:
                            seen_entity_ids.add(rec.entity_id)
                            results.append(rec)
                            if len(results) >= limit:
                                return results
            except Exception as exc:
                logger.warning("DynamoDB resolve_entities GSI1 query failed: %s", exc)

        return results

    def get_assertions_for_entity(
        self,
        workspace_id: str,
        entity_id: str,
        property_key: str | None = None,
        active_document_ids: Sequence[str] | None = None,
        limit: int = 20,
    ) -> list[OkfPropertyAssertion]:
        _, table = self._get_resources()
        from boto3.dynamodb.conditions import Key

        pk = f"WORKSPACE#{workspace_id}"
        sk_prefix = f"ENTITY#{entity_id}#ASSERTION#"

        key_cond = Key("PK").eq(pk) & Key("SK").begins_with(sk_prefix)
        norm_k = normalize_property_key(property_key) if property_key else None
        active_docs = set(active_document_ids) if active_document_ids is not None else None

        assertions: list[OkfPropertyAssertion] = []
        exclusive_start_key = None

        try:
            while len(assertions) < limit:
                kwargs: dict[str, Any] = {
                    "KeyConditionExpression": key_cond,
                    "Limit": min(limit * 2, 100),
                }
                if exclusive_start_key:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key

                resp = table.query(**kwargs)
                items = resp.get("Items", [])
                for it in items:
                    data_json = it.get("data_json")
                    if not data_json:
                        continue
                    ass = OkfPropertyAssertion(**json.loads(data_json))
                    if norm_k and ass.property_key != norm_k:
                        continue
                    if active_docs is not None and ass.document_id not in active_docs:
                        continue
                    assertions.append(ass)
                    if len(assertions) >= limit:
                        break

                exclusive_start_key = resp.get("LastEvaluatedKey")
                if not exclusive_start_key:
                    break

            return assertions
        except Exception as exc:
            logger.warning("DynamoDB get_assertions_for_entity failed: %s", exc)
            return []

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> bool:
        _, table = self._get_resources()
        from boto3.dynamodb.conditions import Key

        try:
            resp = table.query(
                IndexName="GSI2",
                KeyConditionExpression=Key("GSI2PK").eq(f"WORKSPACE#{workspace_id}#DOC#{document_id}")
                & Key("GSI2SK").begins_with(f"VER#{document_version}#"),
                Limit=1,
            )
            return len(resp.get("Items", [])) > 0
        except Exception as exc:
            logger.warning("DynamoDB has_ready_okf GSI2 query failed: %s", exc)
            return False

    def list_assertions_by_document(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        limit: int = 50,
        next_token: str | None = None,
    ) -> tuple[list[OkfPropertyAssertion], str | None]:
        _, table = self._get_resources()
        from boto3.dynamodb.conditions import Attr, Key

        kwargs: dict[str, Any] = {
            "IndexName": "GSI2",
            "KeyConditionExpression": Key("GSI2PK").eq(f"WORKSPACE#{workspace_id}#DOC#{document_id}")
            & Key("GSI2SK").begins_with(f"VER#{document_version}#ASSERTION#"),
            "Limit": limit,
        }
        if next_token:
            try:
                kwargs["ExclusiveStartKey"] = json.loads(next_token)
            except Exception:
                pass

        try:
            resp = table.query(**kwargs)
            items = resp.get("Items", [])
            assertions = [OkfPropertyAssertion(**json.loads(it["data_json"])) for it in items]
            lek = resp.get("LastEvaluatedKey")
            token = json.dumps(lek) if lek else None
            return assertions, token
        except Exception as exc:
            logger.warning("DynamoDB list_assertions_by_document GSI2 query failed: %s", exc)
            return [], None

    def get_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
    ) -> OkfManifest | None:
        _, table = self._get_resources()
        try:
            resp = table.get_item(
                Key={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"MANIFEST#DOC#{document_id}#VER#{document_version}",
                }
            )
            item = resp.get("Item")
            if not item:
                return None
            return OkfManifest(**json.loads(item["data_json"]))
        except Exception as exc:
            logger.warning("DynamoDB get_manifest failed: %s", exc)
            return None

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        # Note: Scoped deletion in DynamoDB is done per version or deferred
        pass


_SHARED_OKF_REPO: OkfRepositoryBase | None = None
_REPO_LOCK = threading.Lock()


def get_okf_repo(reset: bool = False, require_durable: bool = False) -> OkfRepositoryBase:
    """Resolve and return singleton OKF repository matching configured environment."""
    global _SHARED_OKF_REPO
    with _REPO_LOCK:
        if reset:
            _SHARED_OKF_REPO = None
        elif _SHARED_OKF_REPO is not None:
            return _SHARED_OKF_REPO

        backend_override = getattr(settings, "OKF_REPO_BACKEND", None)
        env = getattr(settings, "ENVIRONMENT", "dev").lower()

        if backend_override == "dynamodb":
            _SHARED_OKF_REPO = DynamoDBOkfRepository()
        elif backend_override == "sqlite":
            _SHARED_OKF_REPO = SQLiteOkfRepository()
        elif backend_override == "memory":
            _SHARED_OKF_REPO = InMemoryOkfRepository()
        else:
            if env in ("prod", "production"):
                _SHARED_OKF_REPO = DynamoDBOkfRepository()
            elif require_durable or env == "dev":
                _SHARED_OKF_REPO = SQLiteOkfRepository()
            else:
                _SHARED_OKF_REPO = InMemoryOkfRepository()

    return _SHARED_OKF_REPO


class OkfRepositoryProxy:
    """Dynamic proxy that forwards calls to get_okf_repo."""

    def __getattr__(self, name: str) -> Any:
        repo = get_okf_repo()
        return getattr(repo, name)


class OkfRepository:
    """Factory class that constructs and returns the configured OKF repository instance."""

    def __new__(cls, *args: Any, **kwargs: Any) -> OkfRepositoryBase:
        return get_okf_repo()


okf_repo = OkfRepositoryProxy()

__all__ = [
    "OkfRepositoryBase",
    "InMemoryOkfRepository",
    "SQLiteOkfRepository",
    "DynamoDBOkfRepository",
    "OkfRepositoryProxy",
    "OkfRepository",
    "get_okf_repo",
    "okf_repo",
    "create_dedicated_storage_config",
    "disable_client_retries",
]

import sys
if __name__ in ("db.okf_repo", "src.db.okf_repo"):
    sys.modules["db.okf_repo"] = sys.modules[__name__]
    sys.modules["src.db.okf_repo"] = sys.modules[__name__]

