"""Partitioned repository for Open Knowledge Framework (OKF) facts.

Provides workspace-partitioned storage for OkfEntityRecord, OkfPropertyAssertion,
and OkfManifest with GSI1 (entity resolution) and GSI2 (document readiness and scoping).
Supports in-memory test storage, durable SQLite persistence, and DynamoDB.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

from config import settings
from src.schemas.contracts.boundary import StorageUnavailableError
from src.schemas.contracts.okf import (
    ExtractionReconciliationResult,
    ExtractionReservationResult,
    OkfBudgetLedger,
    OkfEntityRecord,
    OkfManifest,
    OkfPropertyAssertion,
    generate_publication_id,
    normalize_entity_name,
    normalize_property_key,
)

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def save_manifest(self, workspace_id: str, manifest: OkfManifest, fence_token: int | None = None) -> None:
        """Atomically persist manifest entities and validated assertions under workspace PK."""
        ...

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
        active_document_versions: Mapping[str, str] | None = None,
        active_publications: Mapping[str, str | None] | None = None,
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
        publication_id: str | None = None,
    ) -> list[OkfPropertyAssertion]:
        """Query Main table by PK=WORKSPACE#{ws_id} and SK begins_with ENTITY#{entity_id}."""
        ...

    def has_ready_okf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str = "v1",
    ) -> bool:
        """Query authoritative manifest to verify whether document version has published ready state."""
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
        consistent_read: bool = True,
    ) -> OkfManifest | None:
        """Retrieve manifest for a given workspace and document version using strongly consistent reads."""
        ...

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        """Delete all entities and assertions for a document or document version."""
        ...

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 600,
        max_calls: int = 2,
        max_total_tokens: int = 5200,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1200,
    ) -> ExtractionReservationResult:
        """Atomically reserve an extraction call permit and token allocation."""
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
    ) -> ExtractionReconciliationResult:
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

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> OkfBudgetLedger | None:
        """Retrieve durable budget ledger using strongly consistent reads."""
        ...

    def reset_fixtures(self) -> None:
        """Reset all fixtures in memory / storage."""
        ...


def _anchor_entity_to_manifest(
    ent: OkfEntityRecord,
    workspace_id: str,
    document_id: str,
    document_version: str,
    publication_id: str | None = None,
) -> OkfEntityRecord:
    target_pub = publication_id or getattr(ent, "publication_id", None)
    if (
        ent.document_id != document_id
        or ent.workspace_id != workspace_id
        or ent.document_version != document_version
        or getattr(ent, "publication_id", None) != target_pub
    ):
        return OkfEntityRecord(
            workspace_id=workspace_id,
            entity_id=ent.entity_id,
            entity_name=ent.entity_name,
            normalized_name=ent.normalized_name,
            entity_type=ent.entity_type,
            document_id=document_id,
            document_version=document_version,
            created_at=ent.created_at,
            publication_id=target_pub,
        )
    return ent


def _anchor_assertion_to_manifest(
    ass: OkfPropertyAssertion,
    workspace_id: str,
    document_id: str,
    document_version: str,
    publication_id: str | None = None,
) -> OkfPropertyAssertion:
    target_pub = publication_id or getattr(ass, "publication_id", None)
    if (
        ass.document_id != document_id
        or ass.workspace_id != workspace_id
        or ass.document_version != document_version
        or getattr(ass, "publication_id", None) != target_pub
    ):
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
            publication_id=target_pub,
        )
    return ass


class InMemoryOkfRepository:
    """Thread safe in memory OKF repository strictly for isolated test suites."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[tuple[str, str, str, str, str], OkfEntityRecord] = {}  # (ws, entity_id, doc_id, ver, pub_id)
        self._assertions: dict[tuple[str, str, str, str], OkfPropertyAssertion] = {}  # (ws, entity_id, assertion_id, pub_id)
        self._manifests: dict[tuple[str, str, str], OkfManifest] = {}  # (ws, doc_id, ver)
        self._ledgers: dict[tuple[str, str, str], OkfBudgetLedger] = {}  # (ws, doc_id, ver)

    def reset_fixtures(self) -> None:
        with self._lock:
            self._entities.clear()
            self._assertions.clear()
            self._manifests.clear()
            self._ledgers.clear()

    def save_manifest(self, workspace_id: str, manifest: OkfManifest, fence_token: int | None = None) -> None:
        with self._lock:
            key = (workspace_id, manifest.document_id, manifest.document_version)
            ledger = self._ledgers.get(key)
            if fence_token is not None:
                if ledger is not None and ledger.fence_token > fence_token:
                    raise RuntimeError(f"Worker fenced out: fence token {fence_token} superseded by {ledger.fence_token}")
            else:
                now = time.time()
                if ledger is not None:
                    lease_exp = float(ledger.lease_expires_at or 0)
                    if ledger.lease_owner_id and lease_exp > now:
                        raise RuntimeError(f"Unfenced manifest write denied: active lease held on document {manifest.document_id}")
                    if manifest.status in ("failed", "empty"):
                        raise RuntimeError(f"Unfenced manifest write denied: document {manifest.document_id} has budget ledger; requires fence token")
                existing_man = self._manifests.get(key)
                if existing_man is not None and existing_man.status == "ready" and manifest.status != "ready":
                    raise RuntimeError(f"Unfenced manifest write denied: cannot overwrite ready manifest with {manifest.status} status")

            pub_id = manifest.publication_id or generate_publication_id()
            if manifest.publication_id is None:
                manifest = manifest.model_copy(update={"publication_id": pub_id})

            staged_entities = {}
            for ent in manifest.entities:
                ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                staged_entities[(workspace_id, ent.entity_id, ent.document_id, ent.document_version, pub_id)] = ent
            staged_assertions = {}
            for ass in manifest.assertions:
                ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                staged_assertions[(workspace_id, ass.entity_id, ass.assertion_id, pub_id)] = ass

            self._entities.update(staged_entities)
            self._assertions.update(staged_assertions)
            # Commit manifest visibility last
            self._manifests[key] = manifest

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
        active_document_versions: Mapping[str, str] | None = None,
        active_publications: Mapping[str, str | None] | None = None,
    ) -> list[OkfEntityRecord]:
        with self._lock:
            norm_candidates = {normalize_entity_name(name) for name in candidate_names if name}
            if not norm_candidates:
                return []

            matched: list[OkfEntityRecord] = []
            seen_keys: set[tuple[str, str, str, str | None]] = set()
            for ent in self._entities.values():
                if ent.workspace_id != workspace_id:
                    continue
                if active_document_versions is not None:
                    if ent.document_id not in active_document_versions:
                        continue
                    if active_document_versions[ent.document_id] != ent.document_version:
                        continue
                if active_publications is not None:
                    if ent.document_id not in active_publications:
                        continue
                    expected_pub = active_publications[ent.document_id]
                    ent_pub = getattr(ent, "publication_id", None)
                    if expected_pub is not None:
                        if ent_pub != expected_pub:
                            continue
                    else:
                        if ent_pub is not None:
                            continue
                ent_norm = ent.normalized_name
                # Check exact or prefix match against any candidate
                for cand in norm_candidates:
                    if ent_norm == cand or ent_norm.startswith(cand):
                        k = (ent.entity_id, ent.document_id, ent.document_version, getattr(ent, "publication_id", None))
                        if k not in seen_keys:
                            seen_keys.add(k)
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
        publication_id: str | None = None,
    ) -> list[OkfPropertyAssertion]:
        with self._lock:
            active_docs = set(active_document_ids) if active_document_ids is not None else None
            norm_key = normalize_property_key(property_key) if property_key else None

            matched: list[OkfPropertyAssertion] = []
            for ass in self._assertions.values():
                if ass.workspace_id != workspace_id or ass.entity_id != entity_id:
                    continue
                if publication_id is not None and getattr(ass, "publication_id", None) and ass.publication_id != publication_id:
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
        document_version: str = "v1",
    ) -> bool:
        with self._lock:
            man = self._manifests.get((workspace_id, document_id, document_version))
            return bool(man and man.status == "ready")

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
                ass for ass in self._assertions.values()
                if ass.workspace_id == workspace_id and ass.document_id == document_id and ass.document_version == document_version
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
        consistent_read: bool = True,
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
            self._ledgers = {
                k: v for k, v in self._ledgers.items()
                if not (k[0] == workspace_id and k[1] == document_id and (document_version is None or k[2] == document_version))
            }

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 600,
        max_calls: int = 2,
        max_total_tokens: int = 5200,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1200,
    ) -> ExtractionReservationResult:
        with self._lock:
            key = (workspace_id, document_id, document_version)
            ledger = self._ledgers.get(key)
            now = time.time()
            est_tokens = estimated_prompt_tokens + max_output_tokens
            p_thresh = max_input_tokens - estimated_prompt_tokens
            o_thresh = max_output_ceiling - max_output_tokens
            t_thresh = max_total_tokens - est_tokens

            if max_calls < 1:
                return ExtractionReservationResult(
                    granted=False,
                    reason="calls_exhausted",
                    ledger=ledger,
                )

            if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
                return ExtractionReservationResult(
                    granted=False,
                    reason="tokens_exhausted",
                    ledger=ledger,
                )

            if ledger is not None:
                if ledger.status == "completed":
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_completed",
                        ledger=ledger,
                    )
                if ledger.status == "exhausted":
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_exhausted",
                        ledger=ledger,
                    )
                if ledger.status == "failed":
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=ledger.fence_token,
                        call_number=ledger.reserved_calls,
                        remaining_calls=0,
                        reason="already_failed",
                        ledger=ledger,
                    )
                if ledger.reserved_calls >= max_calls:
                    updated = OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=ledger.reserved_calls,
                        completed_calls=ledger.completed_calls,
                        reserved_tokens=ledger.reserved_tokens,
                        spent_tokens=ledger.spent_tokens,
                        reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                        reserved_output_tokens=ledger.reserved_output_tokens,
                        spent_prompt_tokens=ledger.spent_prompt_tokens,
                        spent_output_tokens=ledger.spent_output_tokens,
                        lease_owner_id=ledger.lease_owner_id,
                        lease_expires_at=ledger.lease_expires_at,
                        fence_token=ledger.fence_token,
                        reconciled_fences=ledger.reconciled_fences,
                        status="exhausted",
                        updated_at=_now_utc(),
                    )
                    self._ledgers[key] = updated
                    return ExtractionReservationResult(
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
                ):
                    updated = OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=ledger.reserved_calls,
                        completed_calls=ledger.completed_calls,
                        reserved_tokens=ledger.reserved_tokens,
                        spent_tokens=ledger.spent_tokens,
                        reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                        reserved_output_tokens=ledger.reserved_output_tokens,
                        spent_prompt_tokens=ledger.spent_prompt_tokens,
                        spent_output_tokens=ledger.spent_output_tokens,
                        lease_owner_id=ledger.lease_owner_id,
                        lease_expires_at=ledger.lease_expires_at,
                        fence_token=ledger.fence_token,
                        reconciled_fences=ledger.reconciled_fences,
                        status="exhausted",
                        updated_at=_now_utc(),
                    )
                    self._ledgers[key] = updated
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=updated.fence_token,
                        call_number=updated.reserved_calls,
                        remaining_calls=0,
                        reason="tokens_exhausted",
                        ledger=updated,
                    )
                if ledger.lease_expires_at > now and ledger.lease_owner_id != worker_id:
                    return ExtractionReservationResult(
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
                updated = OkfBudgetLedger(
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
                self._ledgers[key] = updated
                return ExtractionReservationResult(
                    granted=True,
                    fence_token=new_fence,
                    call_number=new_calls,
                    remaining_calls=max_calls - new_calls,
                    reason="granted",
                    ledger=updated,
                )
            else:
                new_ledger = OkfBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=1,
                    completed_calls=0,
                    reserved_tokens=est_tokens,
                    spent_tokens=0,
                    reserved_prompt_tokens=estimated_prompt_tokens,
                    reserved_output_tokens=max_output_tokens,
                    spent_prompt_tokens=0,
                    spent_output_tokens=0,
                    lease_owner_id=worker_id,
                    lease_expires_at=now + lease_duration_s,
                    fence_token=1,
                    reconciled_fences=(),
                    status="active",
                    updated_at=_now_utc(),
                )
                self._ledgers[key] = new_ledger
                return ExtractionReservationResult(
                    granted=True,
                    fence_token=1,
                    call_number=1,
                    remaining_calls=max_calls - 1,
                    reason="granted",
                    ledger=new_ledger,
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
    ) -> ExtractionReconciliationResult:
        with self._lock:
            key = (workspace_id, document_id, document_version)
            ledger = self._ledgers.get(key)
            if ledger is None:
                return ExtractionReconciliationResult(
                    reconciled=False,
                    fenced_out=True,
                    reason="not_found",
                    ledger=None,
                )
            if fence_token in ledger.reconciled_fences:
                return ExtractionReconciliationResult(
                    reconciled=True,
                    fenced_out=False,
                    reason="already_reconciled",
                    ledger=ledger,
                )
            now = time.time()
            actual_total = actual_prompt_tokens + actual_output_tokens

            if ledger.fence_token != fence_token or ledger.lease_owner_id != worker_id or ledger.lease_expires_at < now:
                new_reconciled = ledger.reconciled_fences + (fence_token,)
                updated_spent = ledger.spent_tokens + actual_total
                updated_spt = ledger.spent_prompt_tokens + actual_prompt_tokens
                updated_sot = ledger.spent_output_tokens + actual_output_tokens
                stale_status = "exhausted" if ledger.reserved_calls >= 2 else ledger.status
                fenced_ledger = OkfBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=ledger.reserved_calls,
                    completed_calls=ledger.completed_calls,
                    reserved_tokens=ledger.reserved_tokens,
                    spent_tokens=updated_spent,
                    reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                    reserved_output_tokens=ledger.reserved_output_tokens,
                    spent_prompt_tokens=updated_spt,
                    spent_output_tokens=updated_sot,
                    lease_owner_id=ledger.lease_owner_id if ledger.fence_token != fence_token else None,
                    lease_expires_at=ledger.lease_expires_at if ledger.fence_token != fence_token else 0.0,
                    fence_token=ledger.fence_token,
                    reconciled_fences=new_reconciled,
                    status=stale_status,
                    updated_at=_now_utc(),
                )
                self._ledgers[key] = fenced_ledger
                return ExtractionReconciliationResult(
                    reconciled=False,
                    fenced_out=True,
                    reason="fenced_out",
                    ledger=fenced_ledger,
                )

            new_completed = ledger.completed_calls + 1
            new_spent = ledger.spent_tokens + actual_total
            new_spt = ledger.spent_prompt_tokens + actual_prompt_tokens
            new_sot = ledger.spent_output_tokens + actual_output_tokens
            new_reconciled = ledger.reconciled_fences + (fence_token,)
            reconciled_ledger = OkfBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=ledger.reserved_calls,
                completed_calls=new_completed,
                reserved_tokens=ledger.reserved_tokens,
                spent_tokens=new_spent,
                reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                reserved_output_tokens=ledger.reserved_output_tokens,
                spent_prompt_tokens=new_spt,
                spent_output_tokens=new_sot,
                lease_owner_id=None,
                lease_expires_at=0.0,
                fence_token=ledger.fence_token,
                reconciled_fences=new_reconciled,
                status=status,
                updated_at=_now_utc(),
            )
            self._ledgers[key] = reconciled_ledger
            return ExtractionReconciliationResult(
                reconciled=True,
                fenced_out=False,
                reason="reconciled",
                ledger=reconciled_ledger,
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
        with self._lock:
            key = (workspace_id, document_id, document_version)
            ledger = self._ledgers.get(key)
            if ledger is None:
                return
            if fence_token in ledger.reconciled_fences:
                return
            actual_spent = prompt_tokens_spent + output_tokens_spent
            new_spent = ledger.spent_tokens + actual_spent
            new_spt = ledger.spent_prompt_tokens + prompt_tokens_spent
            new_sot = ledger.spent_output_tokens + output_tokens_spent
            new_reconciled = ledger.reconciled_fences + (fence_token,)
            new_status = "failed"
            lease_owner = None if ledger.lease_owner_id == worker_id else ledger.lease_owner_id
            lease_exp = 0.0 if ledger.lease_owner_id == worker_id else ledger.lease_expires_at
            updated = OkfBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=ledger.reserved_calls,
                completed_calls=ledger.completed_calls,
                reserved_tokens=ledger.reserved_tokens,
                spent_tokens=new_spent,
                reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                reserved_output_tokens=ledger.reserved_output_tokens,
                spent_prompt_tokens=new_spt,
                spent_output_tokens=new_sot,
                lease_owner_id=lease_owner,
                lease_expires_at=lease_exp,
                fence_token=ledger.fence_token,
                reconciled_fences=new_reconciled,
                status=new_status,
                updated_at=_now_utc(),
            )
            self._ledgers[key] = updated

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> OkfBudgetLedger | None:
        with self._lock:
            key = (workspace_id, document_id, document_version)
            ledger = self._ledgers.get(key)
            if ledger is not None:
                return ledger
            man = self.get_manifest(workspace_id, document_id, document_version)
            if man is not None:
                return OkfBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=getattr(man, "model_calls", 0),
                    completed_calls=getattr(man, "model_calls", 0),
                    reserved_tokens=getattr(man, "extraction_tokens", 0),
                    spent_tokens=getattr(man, "extraction_tokens", 0),
                    status="failed" if getattr(man, "status", "") == "failed" else "completed",
                    updated_at=getattr(man, "created_at", _now_utc()),
                )
            return None


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

    def save_manifest(self, workspace_id: str, manifest: OkfManifest, fence_token: int | None = None) -> None:
        with self._lock:
            with self._conn:
                pub_id = manifest.publication_id or generate_publication_id()
                if manifest.publication_id is None:
                    manifest = manifest.model_copy(update={"publication_id": pub_id})

                if fence_token is not None:
                    pk = f"WORKSPACE#{workspace_id}"
                    sk = f"BUDGET#DOC#{manifest.document_id}#VER#{manifest.document_version}"
                    cur = self._conn.execute("SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?", (pk, sk))
                    row = cur.fetchone()
                    if row:
                        ledger_data = json.loads(row["data_json"])
                        curr_fence = int(ledger_data.get("fence_token", 0))
                        if curr_fence > fence_token:
                            raise RuntimeError(f"Worker fenced out: fence token {fence_token} superseded by {curr_fence}")

                # 1. Save manifest record
                man_pk = f"WORKSPACE#{workspace_id}"
                man_sk = f"MANIFEST#DOC#{manifest.document_id}#VER#{manifest.document_version}"
                gsi1_pk = man_pk
                gsi1_sk = f"MANIFEST#{manifest.document_id}"
                gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{manifest.document_id}"
                gsi2_sk = f"VER#{manifest.document_version}#MANIFEST"

                if fence_token is not None:
                    bpk = f"WORKSPACE#{workspace_id}"
                    bsk = f"BUDGET#DOC#{manifest.document_id}#VER#{manifest.document_version}"
                    cur = self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        )
                        SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM kre_okf_facts
                            WHERE pk = ? AND sk = ?
                            AND CAST(json_extract(data_json, '$.fence_token') AS INTEGER) > ?
                        )
                        """,
                        (
                            workspace_id, man_pk, man_sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            "MANIFEST", None, manifest.document_id, manifest.document_version,
                            None, None, manifest.model_dump_json(), manifest.created_at,
                            bpk, bsk, fence_token,
                        ),
                    )
                    if cur.rowcount == 0:
                        raise RuntimeError(f"Worker fenced out: fence token {fence_token} superseded")
                else:
                    bpk = f"WORKSPACE#{workspace_id}"
                    bsk = f"BUDGET#DOC#{manifest.document_id}#VER#{manifest.document_version}"
                    now_ts = time.time()

                    # 1. Active lease check & budget ledger protection
                    budget_cur = self._conn.execute(
                        "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                        (bpk, bsk),
                    )
                    budget_row = budget_cur.fetchone()
                    if budget_row:
                        try:
                            bdata = json.loads(budget_row["data_json"])
                            lease_exp = float(bdata.get("lease_expires_at") or 0)
                            lease_owner = bdata.get("lease_owner_id")
                            if lease_owner and lease_exp > now_ts:
                                raise RuntimeError(f"Unfenced manifest write denied: active lease held on document {manifest.document_id}")
                            if manifest.status in ("failed", "empty"):
                                raise RuntimeError(f"Unfenced manifest write denied: document {manifest.document_id} has budget ledger; requires fence token")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

                    # 2. Existing ready manifest protection
                    man_cur = self._conn.execute(
                        "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                        (man_pk, man_sk),
                    )
                    man_row = man_cur.fetchone()
                    if man_row:
                        try:
                            mdata = json.loads(man_row["data_json"])
                            if mdata.get("status") == "ready" and manifest.status != "ready":
                                raise RuntimeError(f"Unfenced manifest write denied: cannot overwrite ready manifest with {manifest.status} status")
                        except RuntimeError:
                            raise
                        except Exception:
                            pass

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

                pub_id = manifest.publication_id or manifest.idempotency_key

                # 2. Save entity records
                for ent in manifest.entities:
                    ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                    ent_pk = f"WORKSPACE#{workspace_id}"
                    ent_sk = f"ENTITY#{ent.entity_id}#DOC#{ent.document_id}#VER#{ent.document_version}#PUB#{pub_id}"
                    ent_gsi1_pk = ent_pk
                    ent_gsi1_sk = f"ENTITY_NAME#{ent.normalized_name}"
                    ent_gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{ent.document_id}"
                    ent_gsi2_sk = f"VER#{ent.document_version}#PUB#{pub_id}#ENTITY#{ent.entity_id}"
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
                    ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                    ass_pk = f"WORKSPACE#{workspace_id}"
                    ass_sk = f"ENTITY#{ass.entity_id}#ASSERTION#PUB#{pub_id}#{ass.assertion_id}"
                    ass_gsi1_pk = ass_pk
                    ass_gsi1_sk = f"ASSERTION#{ass.assertion_id}#PUB#{pub_id}"
                    ass_gsi2_pk = f"WORKSPACE#{workspace_id}#DOC#{ass.document_id}"
                    ass_gsi2_sk = f"VER#{ass.document_version}#ASSERTION#PUB#{pub_id}#{ass.assertion_id}"
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
        active_document_versions: Mapping[str, str] | None = None,
        active_publications: Mapping[str, str | None] | None = None,
    ) -> list[OkfEntityRecord]:
        with self._lock:
            norm_candidates = [normalize_entity_name(name) for name in candidate_names if name]
            if not norm_candidates:
                return []

            gsi1_pk = f"WORKSPACE#{workspace_id}"
            results: list[OkfEntityRecord] = []
            seen_keys: set[tuple[str, str, str, str | None]] = set()

            for cand in norm_candidates:
                # Query GSI1 by normalized name prefix or exact match
                cursor = self._conn.execute(
                    """
                    SELECT data_json FROM kre_okf_facts
                    WHERE gsi1_pk = ? AND record_type = 'ENTITY' AND gsi1_sk LIKE ?
                    """,
                    (gsi1_pk, f"ENTITY_NAME#{cand}%"),
                )
                for row in cursor:
                    data = json.loads(row["data_json"])
                    rec = OkfEntityRecord(**data)
                    if active_document_versions is not None:
                        if rec.document_id not in active_document_versions:
                            continue
                        if active_document_versions[rec.document_id] != rec.document_version:
                            continue
                    if active_publications is not None:
                        if rec.document_id not in active_publications:
                            continue
                        expected_pub = active_publications[rec.document_id]
                        ent_pub = getattr(rec, "publication_id", None)
                        if expected_pub is not None:
                            if ent_pub != expected_pub:
                                continue
                        else:
                            if ent_pub is not None:
                                continue
                    k = (rec.entity_id, rec.document_id, rec.document_version, getattr(rec, "publication_id", None))
                    if k not in seen_keys:
                        seen_keys.add(k)
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
        publication_id: str | None = None,
    ) -> list[OkfPropertyAssertion]:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            if publication_id:
                sk_pattern = f"ENTITY#{entity_id}#ASSERTION#PUB#{publication_id}#%"
            else:
                sk_pattern = f"ENTITY#{entity_id}#ASSERTION#%"

            query = (
                "SELECT data_json FROM kre_okf_facts "
                "WHERE pk = ? AND sk LIKE ? AND record_type = 'ASSERTION'"
            )
            params: list[Any] = [pk, sk_pattern]

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
        document_version: str = "v1",
    ) -> bool:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            sk = f"MANIFEST#DOC#{document_id}#VER#{document_version}"
            cursor = self._conn.execute(
                "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                (pk, sk),
            )
            row = cursor.fetchone()
            if not row:
                return False
            try:
                data = json.loads(row["data_json"])
                return data.get("status") == "ready"
            except Exception:
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
        consistent_read: bool = True,
    ) -> OkfManifest | None:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            sk = f"MANIFEST#DOC#{document_id}#VER#{document_version}"
            try:
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                return OkfManifest(**json.loads(row["data_json"]))
            except Exception as exc:
                logger.error("SQLite get_manifest failed: %s", exc)
                raise StorageUnavailableError(f"SQLite error reading OKF manifest: {exc}") from exc

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

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 600,
        max_calls: int = 2,
        max_total_tokens: int = 5200,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1200,
    ) -> ExtractionReservationResult:
        with self._lock:
            with self._conn:
                pk = f"WORKSPACE#{workspace_id}"
                sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
                now = time.time()
                est_tokens = estimated_prompt_tokens + max_output_tokens
                p_thresh = max_input_tokens - estimated_prompt_tokens
                o_thresh = max_output_ceiling - max_output_tokens
                t_thresh = max_total_tokens - est_tokens

                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                ledger: OkfBudgetLedger | None = None
                if row:
                    ledger = OkfBudgetLedger(**json.loads(row["data_json"]))

                if max_calls < 1:
                    return ExtractionReservationResult(
                        granted=False,
                        reason="calls_exhausted",
                        ledger=ledger,
                    )

                if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
                    return ExtractionReservationResult(
                        granted=False,
                        reason="tokens_exhausted",
                        ledger=ledger,
                    )

                if ledger is not None:
                    if ledger.status == "completed":
                        return ExtractionReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_completed",
                            ledger=ledger,
                        )
                    if ledger.status == "exhausted":
                        return ExtractionReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_exhausted",
                            ledger=ledger,
                        )
                    if ledger.status == "failed":
                        return ExtractionReservationResult(
                            granted=False,
                            fence_token=ledger.fence_token,
                            call_number=ledger.reserved_calls,
                            remaining_calls=0,
                            reason="already_failed",
                            ledger=ledger,
                        )
                    if ledger.reserved_calls >= max_calls:
                        updated = OkfBudgetLedger(
                            workspace_id=workspace_id,
                            document_id=document_id,
                            document_version=document_version,
                            reserved_calls=ledger.reserved_calls,
                            completed_calls=ledger.completed_calls,
                            reserved_tokens=ledger.reserved_tokens,
                            spent_tokens=ledger.spent_tokens,
                            reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                            reserved_output_tokens=ledger.reserved_output_tokens,
                            spent_prompt_tokens=ledger.spent_prompt_tokens,
                            spent_output_tokens=ledger.spent_output_tokens,
                            lease_owner_id=ledger.lease_owner_id,
                            lease_expires_at=ledger.lease_expires_at,
                            fence_token=ledger.fence_token,
                            reconciled_fences=ledger.reconciled_fences,
                            status="exhausted",
                            updated_at=_now_utc(),
                        )
                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO kre_okf_facts (
                                workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                                record_type, entity_id, document_id, document_version,
                                normalized_name, property_key, data_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                                f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                                "BUDGET", None, document_id, document_version, None, None,
                                updated.model_dump_json(), updated.updated_at,
                            ),
                        )
                        return ExtractionReservationResult(
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
                    ):
                        updated = OkfBudgetLedger(
                            workspace_id=workspace_id,
                            document_id=document_id,
                            document_version=document_version,
                            reserved_calls=ledger.reserved_calls,
                            completed_calls=ledger.completed_calls,
                            reserved_tokens=ledger.reserved_tokens,
                            spent_tokens=ledger.spent_tokens,
                            reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                            reserved_output_tokens=ledger.reserved_output_tokens,
                            spent_prompt_tokens=ledger.spent_prompt_tokens,
                            spent_output_tokens=ledger.spent_output_tokens,
                            lease_owner_id=ledger.lease_owner_id,
                            lease_expires_at=ledger.lease_expires_at,
                            fence_token=ledger.fence_token,
                            reconciled_fences=ledger.reconciled_fences,
                            status="exhausted",
                            updated_at=_now_utc(),
                        )
                        self._conn.execute(
                            """
                            INSERT OR REPLACE INTO kre_okf_facts (
                                workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                                record_type, entity_id, document_id, document_version,
                                normalized_name, property_key, data_json, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                                f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                                "BUDGET", None, document_id, document_version, None, None,
                                updated.model_dump_json(), updated.updated_at,
                            ),
                        )
                        return ExtractionReservationResult(
                            granted=False,
                            fence_token=updated.fence_token,
                            call_number=updated.reserved_calls,
                            remaining_calls=0,
                            reason="tokens_exhausted",
                            ledger=updated,
                        )
                    if ledger.lease_expires_at > now and ledger.lease_owner_id != worker_id:
                        return ExtractionReservationResult(
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
                    updated = OkfBudgetLedger(
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
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                            f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                            "BUDGET", None, document_id, document_version, None, None,
                            updated.model_dump_json(), updated.updated_at,
                        ),
                    )
                    return ExtractionReservationResult(
                        granted=True,
                        fence_token=new_fence,
                        call_number=new_calls,
                        remaining_calls=max_calls - new_calls,
                        reason="granted",
                        ledger=updated,
                    )
                else:
                    new_ledger = OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=1,
                        completed_calls=0,
                        reserved_tokens=est_tokens,
                        spent_tokens=0,
                        reserved_prompt_tokens=estimated_prompt_tokens,
                        reserved_output_tokens=max_output_tokens,
                        spent_prompt_tokens=0,
                        spent_output_tokens=0,
                        lease_owner_id=worker_id,
                        lease_expires_at=now + lease_duration_s,
                        fence_token=1,
                        reconciled_fences=(),
                        status="active",
                        updated_at=_now_utc(),
                    )
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                            f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                            "BUDGET", None, document_id, document_version, None, None,
                            new_ledger.model_dump_json(), new_ledger.updated_at,
                        ),
                    )
                    return ExtractionReservationResult(
                        granted=True,
                        fence_token=1,
                        call_number=1,
                        remaining_calls=max_calls - 1,
                        reason="granted",
                        ledger=new_ledger,
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
    ) -> ExtractionReconciliationResult:
        with self._lock:
            with self._conn:
                pk = f"WORKSPACE#{workspace_id}"
                sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if not row:
                    return ExtractionReconciliationResult(
                        reconciled=False,
                        fenced_out=True,
                        reason="not_found",
                        ledger=None,
                    )
                ledger = OkfBudgetLedger(**json.loads(row["data_json"]))
                if fence_token in ledger.reconciled_fences:
                    return ExtractionReconciliationResult(
                        reconciled=True,
                        fenced_out=False,
                        reason="already_reconciled",
                        ledger=ledger,
                    )
                now = time.time()
                actual_total = actual_prompt_tokens + actual_output_tokens

                if ledger.fence_token != fence_token or ledger.lease_owner_id != worker_id or ledger.lease_expires_at < now:
                    new_reconciled = ledger.reconciled_fences + (fence_token,)
                    updated_spent = ledger.spent_tokens + actual_total
                    updated_spt = ledger.spent_prompt_tokens + actual_prompt_tokens
                    updated_sot = ledger.spent_output_tokens + actual_output_tokens
                    stale_status = "exhausted" if ledger.reserved_calls >= 2 else ledger.status
                    fenced_ledger = OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=ledger.reserved_calls,
                        completed_calls=ledger.completed_calls,
                        reserved_tokens=ledger.reserved_tokens,
                        spent_tokens=updated_spent,
                        reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                        reserved_output_tokens=ledger.reserved_output_tokens,
                        spent_prompt_tokens=updated_spt,
                        spent_output_tokens=updated_sot,
                        lease_owner_id=ledger.lease_owner_id if ledger.fence_token != fence_token else None,
                        lease_expires_at=ledger.lease_expires_at if ledger.fence_token != fence_token else 0.0,
                        fence_token=ledger.fence_token,
                        reconciled_fences=new_reconciled,
                        status=stale_status,
                        updated_at=_now_utc(),
                    )
                    self._conn.execute(
                        """
                        INSERT OR REPLACE INTO kre_okf_facts (
                            workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                            record_type, entity_id, document_id, document_version,
                            normalized_name, property_key, data_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                            f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                            "BUDGET", None, document_id, document_version, None, None,
                            fenced_ledger.model_dump_json(), fenced_ledger.updated_at,
                        ),
                    )
                    return ExtractionReconciliationResult(
                        reconciled=False,
                        fenced_out=True,
                        reason="fenced_out",
                        ledger=fenced_ledger,
                    )

                new_completed = ledger.completed_calls + 1
                new_spent = ledger.spent_tokens + actual_total
                new_spt = ledger.spent_prompt_tokens + actual_prompt_tokens
                new_sot = ledger.spent_output_tokens + actual_output_tokens
                new_reconciled = ledger.reconciled_fences + (fence_token,)
                reconciled_ledger = OkfBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=ledger.reserved_calls,
                    completed_calls=new_completed,
                    reserved_tokens=ledger.reserved_tokens,
                    spent_tokens=new_spent,
                    reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                    reserved_output_tokens=ledger.reserved_output_tokens,
                    spent_prompt_tokens=new_spt,
                    spent_output_tokens=new_sot,
                    lease_owner_id=None,
                    lease_expires_at=0.0,
                    fence_token=ledger.fence_token,
                    reconciled_fences=new_reconciled,
                    status=status,
                    updated_at=_now_utc(),
                )
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_okf_facts (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, entity_id, document_id, document_version,
                        normalized_name, property_key, data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                        f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                        "BUDGET", None, document_id, document_version, None, None,
                        reconciled_ledger.model_dump_json(), reconciled_ledger.updated_at,
                    ),
                )
                return ExtractionReconciliationResult(
                    reconciled=True,
                    fenced_out=False,
                    reason="reconciled",
                    ledger=reconciled_ledger,
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
        with self._lock:
            with self._conn:
                pk = f"WORKSPACE#{workspace_id}"
                sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if not row:
                    return
                ledger = OkfBudgetLedger(**json.loads(row["data_json"]))
                if fence_token in ledger.reconciled_fences:
                    return
                actual_spent = prompt_tokens_spent + output_tokens_spent
                new_spent = ledger.spent_tokens + actual_spent
                new_spt = ledger.spent_prompt_tokens + prompt_tokens_spent
                new_sot = ledger.spent_output_tokens + output_tokens_spent
                new_reconciled = ledger.reconciled_fences + (fence_token,)
                new_status = "failed"
                lease_owner = None if ledger.lease_owner_id == worker_id else ledger.lease_owner_id
                lease_exp = 0.0 if ledger.lease_owner_id == worker_id else ledger.lease_expires_at
                updated = OkfBudgetLedger(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    reserved_calls=ledger.reserved_calls,
                    completed_calls=ledger.completed_calls,
                    reserved_tokens=ledger.reserved_tokens,
                    spent_tokens=new_spent,
                    reserved_prompt_tokens=ledger.reserved_prompt_tokens,
                    reserved_output_tokens=ledger.reserved_output_tokens,
                    spent_prompt_tokens=new_spt,
                    spent_output_tokens=new_sot,
                    lease_owner_id=lease_owner,
                    lease_expires_at=lease_exp,
                    fence_token=ledger.fence_token,
                    reconciled_fences=new_reconciled,
                    status=new_status,
                    updated_at=_now_utc(),
                )
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO kre_okf_facts (
                        workspace_id, pk, sk, gsi1_pk, gsi1_sk, gsi2_pk, gsi2_sk,
                        record_type, entity_id, document_id, document_version,
                        normalized_name, property_key, data_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id, pk, sk, pk, f"BUDGET#{document_id}",
                        f"WORKSPACE#{workspace_id}#DOC#{document_id}", f"VER#{document_version}#BUDGET",
                        "BUDGET", None, document_id, document_version, None, None,
                        updated.model_dump_json(), updated.updated_at,
                    ),
                )

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> OkfBudgetLedger | None:
        with self._lock:
            pk = f"WORKSPACE#{workspace_id}"
            sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
            try:
                cursor = self._conn.execute(
                    "SELECT data_json FROM kre_okf_facts WHERE pk = ? AND sk = ?",
                    (pk, sk),
                )
                row = cursor.fetchone()
                if row:
                    return OkfBudgetLedger(**json.loads(row["data_json"]))
                man = self.get_manifest(workspace_id, document_id, document_version, consistent_read=consistent_read)
                if man is not None:
                    return OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=getattr(man, "model_calls", 0),
                        completed_calls=getattr(man, "model_calls", 0),
                        reserved_tokens=getattr(man, "extraction_tokens", 0),
                        spent_tokens=getattr(man, "extraction_tokens", 0),
                        status="failed" if getattr(man, "status", "") == "failed" else "completed",
                        updated_at=getattr(man, "created_at", _now_utc()),
                    )
                return None
            except Exception as exc:
                logger.error("SQLite get_budget_ledger failed: %s", exc)
                raise StorageUnavailableError(f"SQLite error reading OKF budget ledger: {exc}") from exc


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

    def save_manifest(self, workspace_id: str, manifest: OkfManifest, fence_token: int | None = None) -> None:
        pub_id = manifest.publication_id or generate_publication_id()
        if manifest.publication_id is None:
            manifest = manifest.model_copy(update={"publication_id": pub_id})
        if fence_token is not None:
            curr = self.get_budget_ledger(workspace_id, manifest.document_id, manifest.document_version, consistent_read=True)
            if curr is not None and curr.fence_token > fence_token:
                raise RuntimeError(f"Worker fenced out: fence token {fence_token} superseded by {curr.fence_token}")

        client, table = self._get_resources()
        try:
            # Manifest item
            manifest_item = {
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
                "status": manifest.status,
                "publication_id": pub_id,
                "data_json": manifest.model_dump_json(),
                "created_at": manifest.created_at,
            }

            payload_items: list[dict[str, Any]] = []

            # Entities
            for ent in manifest.entities:
                ent = _anchor_entity_to_manifest(ent, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                payload_items.append({
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"ENTITY#{ent.entity_id}#DOC#{ent.document_id}#VER#{ent.document_version}#PUB#{pub_id}",
                    "GSI1PK": f"WORKSPACE#{workspace_id}",
                    "GSI1SK": f"ENTITY_NAME#{ent.normalized_name}",
                    "GSI2PK": f"WORKSPACE#{workspace_id}#DOC#{ent.document_id}",
                    "GSI2SK": f"VER#{ent.document_version}#PUB#{pub_id}#ENTITY#{ent.entity_id}",
                    "record_type": "ENTITY",
                    "workspace_id": workspace_id,
                    "entity_id": ent.entity_id,
                    "document_id": ent.document_id,
                    "document_version": ent.document_version,
                    "normalized_name": ent.normalized_name,
                    "publication_id": pub_id,
                    "data_json": ent.model_dump_json(),
                    "created_at": ent.created_at,
                })

            # Assertions
            for ass in manifest.assertions:
                ass = _anchor_assertion_to_manifest(ass, workspace_id, manifest.document_id, manifest.document_version, publication_id=pub_id)
                payload_items.append({
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"ENTITY#{ass.entity_id}#ASSERTION#PUB#{pub_id}#{ass.assertion_id}",
                    "GSI1PK": f"WORKSPACE#{workspace_id}",
                    "GSI1SK": f"ASSERTION#{ass.assertion_id}#PUB#{pub_id}",
                    "GSI2PK": f"WORKSPACE#{workspace_id}#DOC#{ass.document_id}",
                    "GSI2SK": f"VER#{ass.document_version}#ASSERTION#PUB#{pub_id}#{ass.assertion_id}",
                    "record_type": "ASSERTION",
                    "workspace_id": workspace_id,
                    "entity_id": ass.entity_id,
                    "document_id": ass.document_id,
                    "document_version": ass.document_version,
                    "property_key": normalize_property_key(ass.property_key),
                    "publication_id": pub_id,
                    "data_json": ass.model_dump_json(),
                    "created_at": ass.created_at,
                })

            all_items: list[dict[str, Any]] = [manifest_item] + payload_items
            serializer = TypeSerializer()

            if fence_token is not None:
                if len(all_items) >= 100:
                    raise ValueError(f"Fenced manifest exceeds atomic transaction limit of 99 items: {len(all_items)}")

                condition_check = {
                    "ConditionCheck": {
                        "TableName": self.table_name,
                        "Key": {
                            "PK": {"S": f"WORKSPACE#{workspace_id}"},
                            "SK": {"S": f"BUDGET#DOC#{manifest.document_id}#VER#{manifest.document_version}"},
                        },
                        "ConditionExpression": "attribute_not_exists(PK) OR #ft <= :expected_fence",
                        "ExpressionAttributeNames": {"#ft": "fence_token"},
                        "ExpressionAttributeValues": {":expected_fence": {"N": str(fence_token)}},
                    }
                }
                tx_items = [condition_check]
                for it in all_items:
                    tx_items.append({
                        "Put": {
                            "TableName": self.table_name,
                            "Item": {k: serializer.serialize(v) for k, v in it.items()}
                        }
                    })
                client.transact_write_items(TransactItems=tx_items)
            else:
                now_epoch = int(time.time())
                budget_key = {
                    "PK": {"S": f"WORKSPACE#{workspace_id}"},
                    "SK": {"S": f"BUDGET#DOC#{manifest.document_id}#VER#{manifest.document_version}"},
                }

                if manifest.status in ("failed", "empty"):
                    # Unfenced failed/empty manifest cannot be saved if a budget ledger exists
                    condition_check = {
                        "ConditionCheck": {
                            "TableName": self.table_name,
                            "Key": budget_key,
                            "ConditionExpression": "attribute_not_exists(PK)",
                        }
                    }
                else:
                    # Unfenced ready manifest (e.g. CSV) cannot overwrite if active Bedrock lease is held
                    condition_check = {
                        "ConditionCheck": {
                            "TableName": self.table_name,
                            "Key": budget_key,
                            "ConditionExpression": "attribute_not_exists(PK) OR attribute_not_exists(lease_expires_at) OR lease_expires_at < :now_epoch",
                            "ExpressionAttributeValues": {":now_epoch": {"N": str(now_epoch)}},
                        }
                    }

                manifest_put = {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": {k: serializer.serialize(v) for k, v in manifest_item.items()},
                        "ConditionExpression": "attribute_not_exists(PK) OR :incoming_status = :ready_status OR #status <> :ready_status",
                        "ExpressionAttributeNames": {"#status": "status"},
                        "ExpressionAttributeValues": {
                            ":ready_status": {"S": "ready"},
                            ":incoming_status": {"S": manifest.status},
                        },
                    }
                }

                if len(all_items) < 100:
                    tx_items = [condition_check, manifest_put]
                    for it in payload_items:
                        tx_items.append({
                            "Put": {
                                "TableName": self.table_name,
                                "Item": {k: serializer.serialize(v) for k, v in it.items()}
                            }
                        })
                    client.transact_write_items(TransactItems=tx_items)
                else:
                    # For large items (e.g. CSV with > 98 rows)
                    ledger = self.get_budget_ledger(workspace_id, manifest.document_id, manifest.document_version, consistent_read=True)
                    if ledger and ledger.lease_owner_id and (ledger.lease_expires_at or 0) > now_epoch:
                        raise RuntimeError(f"Unfenced manifest write denied: active lease held on document {manifest.document_id}")
                    if manifest.status in ("failed", "empty") and ledger is not None:
                        raise RuntimeError(f"Unfenced manifest write denied: document {manifest.document_id} has budget ledger; requires fence token")
                    existing_man = self.get_manifest(workspace_id, manifest.document_id, manifest.document_version, consistent_read=True)
                    if existing_man and existing_man.status == "ready" and manifest.status != "ready":
                        raise RuntimeError(f"Unfenced manifest write denied: cannot overwrite ready manifest with {manifest.status} status")

                    # Stage all payload items (entities and assertions) first into distinct publication keys
                    with table.batch_writer() as batch:
                        for it in payload_items:
                            batch.put_item(Item=it)

                    # Atomically validate publication authority and switch visibility to the new publication
                    client.transact_write_items(TransactItems=[condition_check, manifest_put])

        except ClientError as ce:
            code = ce.response.get("Error", {}).get("Code", "")
            if code == "TransactionCanceledException" or "TransactionCanceledException" in str(ce):
                raise RuntimeError(f"Worker fenced out or unfenced write denied: fence={fence_token} status={manifest.status}") from ce
            logger.error("DynamoDB save_manifest failed for doc %s: %s", manifest.document_id, ce)
            raise StorageUnavailableError(f"DynamoDB error saving OKF manifest: {ce}") from ce
        except RuntimeError:
            raise
        except ValueError as ve:
            raise StorageUnavailableError(f"DynamoDB error saving OKF manifest: {ve}") from ve
        except StorageUnavailableError:
            raise
        except Exception as exc:
            if "TransactionCanceledException" in str(exc) or "Worker fenced out" in str(exc) or "Unfenced manifest write denied" in str(exc):
                raise RuntimeError(f"Worker fenced out or unfenced write denied: {exc}") from exc
            logger.error("DynamoDB save_manifest failed for doc %s: %s", manifest.document_id, exc)
            raise StorageUnavailableError(f"DynamoDB error saving OKF manifest: {exc}") from exc

    def resolve_entities(
        self,
        workspace_id: str,
        candidate_names: Sequence[str],
        limit: int = 10,
        active_document_versions: Mapping[str, str] | None = None,
        active_publications: Mapping[str, str | None] | None = None,
    ) -> list[OkfEntityRecord]:
        _, table = self._get_resources()
        norm_candidates = [normalize_entity_name(name) for name in candidate_names if name]
        if not norm_candidates:
            return []

        from boto3.dynamodb.conditions import Key

        results: list[OkfEntityRecord] = []
        seen_keys: set[tuple[str, str, str, str | None]] = set()

        for cand in norm_candidates:
            try:
                query_kwargs: dict[str, Any] = {
                    "IndexName": "GSI1",
                    "KeyConditionExpression": Key("GSI1PK").eq(f"WORKSPACE#{workspace_id}")
                    & Key("GSI1SK").begins_with(f"ENTITY_NAME#{cand}"),
                }
                while True:
                    resp = table.query(**query_kwargs)
                    items = resp.get("Items", [])
                    for it in items:
                        if it.get("record_type") == "ENTITY":
                            rec = OkfEntityRecord(**json.loads(it["data_json"]))
                            if active_document_versions is not None:
                                if rec.document_id not in active_document_versions:
                                    continue
                                if active_document_versions[rec.document_id] != rec.document_version:
                                    continue
                            if active_publications is not None:
                                if rec.document_id not in active_publications:
                                    continue
                                expected_pub = active_publications[rec.document_id]
                                ent_pub = getattr(rec, "publication_id", None)
                                if expected_pub is not None:
                                    if ent_pub != expected_pub:
                                        continue
                                else:
                                    if ent_pub is not None:
                                        continue
                            k = (rec.entity_id, rec.document_id, rec.document_version, getattr(rec, "publication_id", None))
                            if k not in seen_keys:
                                seen_keys.add(k)
                                results.append(rec)
                                if len(results) >= limit:
                                    return results
                    last_key = resp.get("LastEvaluatedKey")
                    if not last_key:
                        break
                    query_kwargs["ExclusiveStartKey"] = last_key
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
        publication_id: str | None = None,
    ) -> list[OkfPropertyAssertion]:
        _, table = self._get_resources()
        from boto3.dynamodb.conditions import Key

        pk = f"WORKSPACE#{workspace_id}"
        if publication_id:
            sk_prefix = f"ENTITY#{entity_id}#ASSERTION#PUB#{publication_id}#"
        else:
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
                    if it.get("record_type") != "ASSERTION":
                        continue
                    data_json = it.get("data_json")
                    if not data_json:
                        continue
                    ass = OkfPropertyAssertion(**json.loads(data_json))
                    if publication_id is not None and getattr(ass, "publication_id", None) and ass.publication_id != publication_id:
                        continue
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
        document_version: str = "v1",
    ) -> bool:
        _, table = self._get_resources()
        pk = f"WORKSPACE#{workspace_id}"
        sk = f"MANIFEST#DOC#{document_id}#VER#{document_version}"
        try:
            resp = table.get_item(
                Key={"PK": pk, "SK": sk},
                ConsistentRead=True,
            )
            item = resp.get("Item")
            if not item:
                return False
            if "status" in item:
                return item["status"] == "ready"
            if "data_json" in item:
                data = json.loads(item["data_json"])
                return data.get("status") == "ready"
            return False
        except Exception as exc:
            logger.error("DynamoDB has_ready_okf get_item failed: %s", exc)
            raise StorageUnavailableError(f"DynamoDB error querying OKF readiness: {exc}") from exc

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
        consistent_read: bool = True,
    ) -> OkfManifest | None:
        _, table = self._get_resources()
        try:
            resp = table.get_item(
                Key={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"MANIFEST#DOC#{document_id}#VER#{document_version}",
                },
                ConsistentRead=consistent_read,
            )
            item = resp.get("Item")
            if not item:
                return None
            return OkfManifest(**json.loads(item["data_json"]))
        except Exception as exc:
            logger.error("DynamoDB get_manifest failed: %s", exc)
            raise StorageUnavailableError(f"DynamoDB error reading OKF manifest: {exc}") from exc

    def delete_document_facts(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str | None = None,
    ) -> None:
        # Note: Scoped deletion in DynamoDB is done per version or deferred
        pass

    def reserve_extraction_budget(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        worker_id: str,
        estimated_prompt_tokens: int,
        max_output_tokens: int = 600,
        max_calls: int = 2,
        max_total_tokens: int = 5200,
        lease_duration_s: float = 15.0,
        max_input_tokens: int = 4000,
        max_output_ceiling: int = 1200,
    ) -> ExtractionReservationResult:
        _, table = self._get_resources()
        pk = f"WORKSPACE#{workspace_id}"
        sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
        now = time.time()
        est_tokens = estimated_prompt_tokens + max_output_tokens
        p_thresh = max_input_tokens - estimated_prompt_tokens
        o_thresh = max_output_ceiling - max_output_tokens
        t_thresh = max_total_tokens - est_tokens

        if max_calls < 1:
            curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
            return ExtractionReservationResult(
                granted=False,
                reason="calls_exhausted",
                ledger=curr,
            )

        if p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
            curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
            return ExtractionReservationResult(
                granted=False,
                reason="tokens_exhausted",
                ledger=curr,
            )

        try:
            resp = table.update_item(
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
                    ":rtype": "BUDGET",
                    ":now_iso": _now_utc(),
                },
                ReturnValues="ALL_NEW",
            )
            attrs = resp.get("Attributes", {})
            raw_rf = attrs.get("reconciled_fences", [])
            reconciled_fences = tuple(int(x) for x in raw_rf) if raw_rf else ()
            ledger = OkfBudgetLedger(
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
            return ExtractionReservationResult(
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
                    if curr.status == "completed":
                        reason = "already_completed"
                    elif curr.status == "exhausted":
                        reason = "already_exhausted"
                    elif curr.status == "failed":
                        reason = "already_failed"
                    elif curr.reserved_calls >= max_calls:
                        reason = "calls_exhausted"
                    elif (
                        curr.reserved_prompt_tokens > p_thresh
                        or curr.reserved_output_tokens > o_thresh
                        or curr.reserved_tokens > t_thresh
                    ):
                        reason = "tokens_exhausted"
                    elif curr.lease_expires_at > now and curr.lease_owner_id != worker_id:
                        reason = "active_lease_held"
                    else:
                        reason = "calls_exhausted"
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=curr.fence_token,
                        call_number=curr.reserved_calls,
                        remaining_calls=max(0, max_calls - curr.reserved_calls),
                        reason=reason,
                        ledger=curr,
                    )
                else:
                    if max_calls < 1:
                        reason = "calls_exhausted"
                    elif p_thresh < 0 or o_thresh < 0 or t_thresh < 0:
                        reason = "tokens_exhausted"
                    else:
                        reason = "calls_exhausted"
                    return ExtractionReservationResult(
                        granted=False,
                        fence_token=0,
                        call_number=0,
                        remaining_calls=0,
                        reason=reason,
                        ledger=None,
                    )
            logger.error("DynamoDB reserve_extraction_budget error: %s", exc)
            return ExtractionReservationResult(
                granted=False,
                reason="storage_error",
                ledger=None,
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
    ) -> ExtractionReconciliationResult:
        _, table = self._get_resources()
        pk = f"WORKSPACE#{workspace_id}"
        sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
        actual_total = actual_prompt_tokens + actual_output_tokens
        now = time.time()

        curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
        if curr is not None and fence_token in curr.reconciled_fences:
            return ExtractionReconciliationResult(
                reconciled=True,
                fenced_out=False,
                reason="already_reconciled",
                ledger=curr,
            )

        try:
            resp = table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=(
                    "SET #comp = #comp + :one, "
                    "#spent = #spent + :actual_tokens, "
                    "#spt = if_not_exists(#spt, :zero) + :actual_prompt, "
                    "#sot = if_not_exists(#sot, :zero) + :actual_output, "
                    "#lexp = :zero, "
                    "#st = :new_status, "
                    "#rf = list_append(if_not_exists(#rf, :empty_list), :ft_list), "
                    "updated_at = :now_iso"
                ),
                ConditionExpression="#ft = :expected_ft AND #lown = :wid AND #lexp >= :now AND (attribute_not_exists(#rf) OR NOT contains(#rf, :expected_ft))",
                ExpressionAttributeNames={
                    "#comp": "completed_calls",
                    "#spent": "spent_tokens",
                    "#spt": "spent_prompt_tokens",
                    "#sot": "spent_output_tokens",
                    "#lexp": "lease_expires_at",
                    "#st": "status",
                    "#ft": "fence_token",
                    "#lown": "lease_owner_id",
                    "#rf": "reconciled_fences",
                },
                ExpressionAttributeValues={
                    ":one": 1,
                    ":actual_tokens": actual_total,
                    ":actual_prompt": actual_prompt_tokens,
                    ":actual_output": actual_output_tokens,
                    ":zero": 0,
                    ":new_status": status,
                    ":expected_ft": fence_token,
                    ":wid": worker_id,
                    ":now": Decimal(str(round(now, 4))),
                    ":empty_list": [],
                    ":ft_list": [fence_token],
                    ":now_iso": _now_utc(),
                },
                ReturnValues="ALL_NEW",
            )
            attrs = resp.get("Attributes", {})
            raw_rf = attrs.get("reconciled_fences", [])
            reconciled_fences = tuple(int(x) for x in raw_rf) if raw_rf else ()
            ledger = OkfBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=int(attrs.get("reserved_calls", 0)),
                completed_calls=int(attrs.get("completed_calls", 0)),
                reserved_tokens=int(attrs.get("reserved_tokens", 0)),
                spent_tokens=int(attrs.get("spent_tokens", 0)),
                reserved_prompt_tokens=int(attrs.get("reserved_prompt_tokens", 0)),
                reserved_output_tokens=int(attrs.get("reserved_output_tokens", 0)),
                spent_prompt_tokens=int(attrs.get("spent_prompt_tokens", 0)),
                spent_output_tokens=int(attrs.get("spent_output_tokens", 0)),
                lease_owner_id=attrs.get("lease_owner_id"),
                lease_expires_at=float(attrs.get("lease_expires_at", 0.0)),
                fence_token=int(attrs.get("fence_token", 0)),
                reconciled_fences=reconciled_fences,
                status=attrs.get("status", status),
                updated_at=attrs.get("updated_at", _now_utc()),
            )
            return ExtractionReconciliationResult(
                reconciled=True,
                fenced_out=False,
                reason="reconciled",
                ledger=ledger,
            )
        except Exception as exc:
            is_cond = False
            if hasattr(exc, "response") and isinstance(exc.response, dict):
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    is_cond = True
            elif "ConditionalCheckFailedException" in str(exc):
                is_cond = True

            if is_cond:
                curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                if curr is not None and fence_token in curr.reconciled_fences:
                    return ExtractionReconciliationResult(
                        reconciled=True,
                        fenced_out=False,
                        reason="already_reconciled",
                        ledger=curr,
                    )
                try:
                    table.update_item(
                        Key={"PK": pk, "SK": sk},
                        UpdateExpression=(
                            "SET #spent = if_not_exists(#spent, :zero) + :actual_tokens, "
                            "#spt = if_not_exists(#spt, :zero) + :actual_prompt, "
                            "#sot = if_not_exists(#sot, :zero) + :actual_output, "
                            "#rf = list_append(if_not_exists(#rf, :empty_list), :ft_list), "
                            "updated_at = :now_iso"
                        ),
                        ConditionExpression="(attribute_not_exists(#rf) OR NOT contains(#rf, :expected_ft))",
                        ExpressionAttributeNames={
                            "#spent": "spent_tokens",
                            "#spt": "spent_prompt_tokens",
                            "#sot": "spent_output_tokens",
                            "#rf": "reconciled_fences",
                        },
                        ExpressionAttributeValues={
                            ":zero": 0,
                            ":actual_tokens": actual_total,
                            ":actual_prompt": actual_prompt_tokens,
                            ":actual_output": actual_output_tokens,
                            ":empty_list": [],
                            ":ft_list": [fence_token],
                            ":expected_ft": fence_token,
                            ":now_iso": _now_utc(),
                        },
                    )
                except Exception as update_err:
                    if "ConditionalCheckFailedException" in str(update_err):
                        curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                        return ExtractionReconciliationResult(
                            reconciled=True,
                            fenced_out=False,
                            reason="already_reconciled",
                            ledger=curr,
                        )
                    logger.warning("Failed to record fenced token spend in DynamoDB: %s", update_err)
                curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
                return ExtractionReconciliationResult(
                    reconciled=False,
                    fenced_out=True,
                    reason="fenced_out",
                    ledger=curr,
                )
            logger.error("DynamoDB reconcile_extraction_budget error: %s", exc)
            return ExtractionReconciliationResult(
                reconciled=False,
                fenced_out=False,
                reason="storage_error",
                ledger=None,
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
        _, table = self._get_resources()
        pk = f"WORKSPACE#{workspace_id}"
        sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"
        actual_spent = prompt_tokens_spent + output_tokens_spent

        curr = self.get_budget_ledger(workspace_id, document_id, document_version, consistent_read=True)
        if curr is not None and fence_token in curr.reconciled_fences:
            return

        try:
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression=(
                    "SET #spent = if_not_exists(#spent, :zero) + :spent, "
                    "#spt = if_not_exists(#spt, :zero) + :spt, "
                    "#sot = if_not_exists(#sot, :zero) + :sot, "
                    "#lexp = :zero, "
                    "#st = :st_fail, "
                    "#rf = list_append(if_not_exists(#rf, :empty_list), :ft_list), "
                    "updated_at = :now_iso"
                ),
                ConditionExpression="(attribute_not_exists(#rf) OR NOT contains(#rf, :expected_ft))",
                ExpressionAttributeNames={
                    "#spent": "spent_tokens",
                    "#spt": "spent_prompt_tokens",
                    "#sot": "spent_output_tokens",
                    "#lexp": "lease_expires_at",
                    "#st": "status",
                    "#rf": "reconciled_fences",
                },
                ExpressionAttributeValues={
                    ":spent": actual_spent,
                    ":spt": prompt_tokens_spent,
                    ":sot": output_tokens_spent,
                    ":zero": 0,
                    ":st_fail": "failed",
                    ":empty_list": [],
                    ":ft_list": [fence_token],
                    ":expected_ft": fence_token,
                    ":now_iso": _now_utc(),
                },
            )
        except Exception as exc:
            if "ConditionalCheckFailedException" in str(exc):
                return
            logger.warning("DynamoDB fail_extraction_budget error: %s", exc)

    def get_budget_ledger(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        consistent_read: bool = True,
    ) -> OkfBudgetLedger | None:
        _, table = self._get_resources()
        pk = f"WORKSPACE#{workspace_id}"
        sk = f"BUDGET#DOC#{document_id}#VER#{document_version}"

        try:
            resp = table.get_item(
                Key={"PK": pk, "SK": sk},
                ConsistentRead=consistent_read,
            )
            item = resp.get("Item")
            if not item:
                man = self.get_manifest(workspace_id, document_id, document_version, consistent_read=consistent_read)
                if man is not None:
                    return OkfBudgetLedger(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        reserved_calls=getattr(man, "model_calls", 0),
                        completed_calls=getattr(man, "model_calls", 0),
                        reserved_tokens=getattr(man, "extraction_tokens", 0),
                        spent_tokens=getattr(man, "extraction_tokens", 0),
                        status="failed" if getattr(man, "status", "") == "failed" else "completed",
                        updated_at=getattr(man, "created_at", _now_utc()),
                    )
                return None
            raw_rf = item.get("reconciled_fences", [])
            reconciled_fences = tuple(int(x) for x in raw_rf) if raw_rf else ()

            reserved_calls = int(item.get("reserved_calls", 0))
            completed_calls = int(item.get("completed_calls", 0))
            reserved_tokens = int(item.get("reserved_tokens", 0))
            spent_tokens = int(item.get("spent_tokens", 0))
            reserved_prompt_tokens = int(item.get("reserved_prompt_tokens", 0))
            reserved_output_tokens = int(item.get("reserved_output_tokens", 0))
            spent_prompt_tokens = int(item.get("spent_prompt_tokens", 0))
            spent_output_tokens = int(item.get("spent_output_tokens", 0))
            status = item.get("status", "active")

            # Fallback for mock items or serialized record payloads containing data_json
            if "data_json" in item and "reserved_calls" not in item:
                try:
                    data = json.loads(item["data_json"])
                    reserved_calls = int(data.get("reserved_calls", data.get("model_calls", 0)))
                    spent_tokens = int(data.get("spent_tokens", data.get("extraction_tokens", 0)))
                    reserved_prompt_tokens = int(data.get("reserved_prompt_tokens", 0))
                    reserved_output_tokens = int(data.get("reserved_output_tokens", 0))
                    spent_prompt_tokens = int(data.get("spent_prompt_tokens", 0))
                    spent_output_tokens = int(data.get("spent_output_tokens", 0))
                    status = data.get("status", "failed" if data.get("status") == "failed" else "active")
                except Exception:
                    pass

            return OkfBudgetLedger(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                reserved_calls=reserved_calls,
                completed_calls=completed_calls,
                reserved_tokens=reserved_tokens,
                spent_tokens=spent_tokens,
                reserved_prompt_tokens=reserved_prompt_tokens,
                reserved_output_tokens=reserved_output_tokens,
                spent_prompt_tokens=spent_prompt_tokens,
                spent_output_tokens=spent_output_tokens,
                lease_owner_id=item.get("lease_owner_id"),
                lease_expires_at=float(item.get("lease_expires_at", 0.0)),
                fence_token=int(item.get("fence_token", 0)),
                reconciled_fences=reconciled_fences,
                status=status,
                updated_at=item.get("updated_at", _now_utc()),
            )
        except Exception as exc:
            logger.error("DynamoDB get_budget_ledger error: %s", exc)
            raise StorageUnavailableError(f"DynamoDB error reading OKF budget ledger: {exc}") from exc


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

