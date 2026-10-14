"""Authoritative repository for workspace boundaries, generation fencing, and tombstones.

Provides storage operations for workspaces, sources, snapshots, background jobs,
and atomic tombstone persistence coordinated with manifest generation.
Supports both in memory test storage and durable SQLite backed persistence.
"""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Protocol, runtime_checkable
import uuid

from config import settings
from src.schemas.contracts.boundary import (
    BenchmarkRunMetadata,
    CASConflictError,
    DocumentSnapshot,
    JobRecord,
    PublicationManifestRecord,
    SnapshotRecord,
    SourceItem,
    StorageUnavailableError,
    TombstoneRecord,
    WorkspaceAccessDeniedError,
    WorkspaceRecord,
)

logger = logging.getLogger(__name__)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@runtime_checkable
class WorkspaceBoundaryRepositoryBase(Protocol):
    """Protocol defining authoritative workspace boundary operations."""

    def reset_fixtures(self) -> None: ...
    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None: ...
    def create_workspace(
        self, name: str, owner_principal_id: str, workspace_id: str | None = None
    ) -> WorkspaceRecord: ...
    def tombstone_workspace(
        self, workspace_id: str, caller_principal_id: str, reason: str = "user_requested"
    ) -> TombstoneRecord: ...
    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool: ...
    def get_tombstone(
        self, workspace_id: str, resource_type: str, resource_id: str
    ) -> TombstoneRecord | None: ...
    def record_source_tombstone(
        self, workspace_id: str, source_id: str, caller_principal_id: str, reason: str = "user_requested"
    ) -> TombstoneRecord: ...
    def get_source(self, workspace_id: str, source_id: str) -> SourceItem | None: ...
    def list_sources(self, workspace_id: str) -> list[SourceItem]: ...
    def create_source(
        self, workspace_id: str, title: str, source_id: str | None = None
    ) -> SourceItem: ...
    def get_snapshot(self, workspace_id: str, snapshot_id: str) -> SnapshotRecord | None: ...
    def save_snapshot(self, snapshot: SnapshotRecord) -> None: ...
    def get_manifest(self, workspace_id: str) -> PublicationManifestRecord | None: ...
    def cas_update_manifest(
        self, workspace_id: str, expected_generation: int, delta: dict[str, Any]
    ) -> PublicationManifestRecord: ...
    def get_job(self, job_id: str) -> JobRecord | None: ...
    def save_job(self, job: JobRecord) -> None: ...
    def save_document_snapshot(self, snapshot: DocumentSnapshot) -> None: ...
    def get_document_snapshot(self, workspace_id: str, document_id: str) -> DocumentSnapshot | None: ...
    def list_document_snapshots(self, workspace_id: str) -> list[DocumentSnapshot]: ...
    def save_benchmark_metadata(self, metadata: BenchmarkRunMetadata) -> None: ...
    def get_benchmark_metadata(self, workspace_id: str, benchmark_run_id: str) -> BenchmarkRunMetadata | None: ...
    def list_benchmark_metadata(self, workspace_id: str) -> list[BenchmarkRunMetadata]: ...


class InMemoryWorkspaceBoundaryRepository:
    """Ephemeral in memory repository strictly for isolated test suites."""

    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._tombstones: dict[tuple[str, str, str], TombstoneRecord] = {}
        self._sources: dict[tuple[str, str], SourceItem] = {}
        self._snapshots: dict[tuple[str, str], SnapshotRecord] = {}
        self._manifests: dict[str, PublicationManifestRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
        self._document_snapshots: dict[tuple[str, str], DocumentSnapshot] = {}
        self._benchmark_metadata: dict[tuple[str, str], BenchmarkRunMetadata] = {}
        self._op_lock = threading.Lock()
        self._seed_fixtures()

    def reset_fixtures(self) -> None:
        """Reset repository state to baseline test fixtures."""
        with self._op_lock:
            self._workspaces.clear()
            self._tombstones.clear()
            self._sources.clear()
            self._snapshots.clear()
            self._manifests.clear()
            self._jobs.clear()
            self._document_snapshots.clear()
            self._benchmark_metadata.clear()
            self._seed_fixtures()

    def _seed_fixtures(self) -> None:
        now = "2026-10-08T00:00:00Z"
        ws_alpha = WorkspaceRecord(
            workspace_id="ws_alpha",
            name="Workspace Alpha",
            owner_principal_id="usr_alice",
            manifest_generation=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._workspaces["ws_alpha"] = ws_alpha
        self._manifests["ws_alpha"] = PublicationManifestRecord(
            workspace_id="ws_alpha",
            manifest_generation=1,
            active_source_ids=(),
            updated_at=now,
        )

        ws_beta = WorkspaceRecord(
            workspace_id="ws_beta",
            name="Workspace Beta",
            owner_principal_id="usr_bob",
            manifest_generation=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self._workspaces["ws_beta"] = ws_beta
        self._manifests["ws_beta"] = PublicationManifestRecord(
            workspace_id="ws_beta",
            manifest_generation=1,
            active_source_ids=(),
            updated_at=now,
        )

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._op_lock:
            return self._workspaces.get(workspace_id)

    def create_workspace(
        self,
        name: str,
        owner_principal_id: str,
        workspace_id: str | None = None,
    ) -> WorkspaceRecord:
        with self._op_lock:
            ws_id = workspace_id or f"ws_{uuid.uuid4().hex[:12]}"
            now = _now_utc()
            record = WorkspaceRecord(
                workspace_id=ws_id,
                name=name,
                owner_principal_id=owner_principal_id,
                manifest_generation=1,
                status="active",
                created_at=now,
                updated_at=now,
            )
            self._workspaces[ws_id] = record
            self._manifests[ws_id] = PublicationManifestRecord(
                workspace_id=ws_id,
                manifest_generation=1,
                active_source_ids=(),
                updated_at=now,
            )
            return record

    def tombstone_workspace(
        self,
        workspace_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        with self._op_lock:
            ws = self._workspaces.get(workspace_id)
            if not ws:
                raise FileNotFoundError(f"Workspace {workspace_id} not found")
            if ws.owner_principal_id != caller_principal_id:
                raise WorkspaceAccessDeniedError("Cannot delete foreign workspace")

            now = _now_utc()
            tombstone = TombstoneRecord(
                workspace_id=workspace_id,
                resource_type="workspace",
                resource_id=workspace_id,
                manifest_generation=ws.manifest_generation + 1,
                tombstoned_at=now,
                deleted_by_principal_id=caller_principal_id,
                reason=reason,
            )
            self._tombstones[(workspace_id, "workspace", workspace_id)] = tombstone
            self._workspaces[workspace_id] = ws.transition_to_tombstoned()
            return tombstone

    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool:
        with self._op_lock:
            return (workspace_id, resource_type, resource_id) in self._tombstones

    def get_tombstone(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
    ) -> TombstoneRecord | None:
        with self._op_lock:
            return self._tombstones.get((workspace_id, resource_type, resource_id))

    def record_source_tombstone(
        self,
        workspace_id: str,
        source_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        with self._op_lock:
            ws = self._workspaces.get(workspace_id)
            if not ws or ws.status == "tombstoned":
                raise FileNotFoundError(f"Workspace {workspace_id} not found")

            existing = self._tombstones.get((workspace_id, "source", source_id))
            if existing:
                return existing

            now = _now_utc()
            next_gen = ws.manifest_generation + 1
            tombstone = TombstoneRecord(
                workspace_id=workspace_id,
                resource_type="source",
                resource_id=source_id,
                manifest_generation=next_gen,
                tombstoned_at=now,
                deleted_by_principal_id=caller_principal_id,
                reason=reason,
            )
            self._tombstones[(workspace_id, "source", source_id)] = tombstone

            if (workspace_id, source_id) in self._sources:
                old_src = self._sources[(workspace_id, source_id)]
                self._sources[(workspace_id, source_id)] = SourceItem(
                    source_id=old_src.source_id,
                    title=old_src.title,
                    status="tombstoned",
                    created_at=old_src.created_at,
                    updated_at=now,
                )

            self._workspaces[workspace_id] = ws.increment_manifest_generation()
            return tombstone

    def get_source(self, workspace_id: str, source_id: str) -> SourceItem | None:
        with self._op_lock:
            if (workspace_id, "source", source_id) in self._tombstones:
                return None
            return self._sources.get((workspace_id, source_id))

    def list_sources(self, workspace_id: str) -> list[SourceItem]:
        with self._op_lock:
            results = []
            for (ws_id, src_id), item in self._sources.items():
                if ws_id == workspace_id:
                    if (workspace_id, "source", src_id) not in self._tombstones:
                        results.append(item)
            return sorted(results, key=lambda s: s.source_id)

    def create_source(
        self,
        workspace_id: str,
        title: str,
        source_id: str | None = None,
    ) -> SourceItem:
        with self._op_lock:
            ws = self._workspaces.get(workspace_id)
            if not ws or ws.status == "tombstoned":
                raise FileNotFoundError(f"Workspace {workspace_id} not found")

            src_id = source_id or f"src_{uuid.uuid4().hex[:12]}"
            now = _now_utc()
            item = SourceItem(
                source_id=src_id,
                title=title,
                status="registered",
                created_at=now,
                updated_at=now,
            )
            self._sources[(workspace_id, src_id)] = item
            return item

    def get_snapshot(self, workspace_id: str, snapshot_id: str) -> SnapshotRecord | None:
        with self._op_lock:
            return self._snapshots.get((workspace_id, snapshot_id))

    def save_snapshot(self, snapshot: SnapshotRecord) -> None:
        with self._op_lock:
            self._snapshots[(snapshot.workspace_id, snapshot.snapshot_id)] = snapshot

    def get_manifest(self, workspace_id: str) -> PublicationManifestRecord | None:
        with self._op_lock:
            return self._manifests.get(workspace_id)

    def cas_update_manifest(
        self,
        workspace_id: str,
        expected_generation: int,
        delta: dict[str, Any],
    ) -> PublicationManifestRecord:
        with self._op_lock:
            ws = self._workspaces.get(workspace_id)
            if not ws or ws.status == "tombstoned":
                raise FileNotFoundError(f"Workspace {workspace_id} not found")

            if ws.manifest_generation != expected_generation:
                raise CASConflictError(
                    f"CAS conflict on workspace {workspace_id}: expected generation {expected_generation}, "
                    f"authoritative generation is {ws.manifest_generation}"
                )

            updated_ws = ws.increment_manifest_generation()
            self._workspaces[workspace_id] = updated_ws

            new_sources = delta.get("active_source_ids", ())
            manifest = PublicationManifestRecord(
                workspace_id=workspace_id,
                manifest_generation=updated_ws.manifest_generation,
                active_source_ids=tuple(new_sources),
                updated_at=_now_utc(),
            )
            self._manifests[workspace_id] = manifest
            return manifest

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._op_lock:
            return self._jobs.get(job_id)

    def save_job(self, job: JobRecord) -> None:
        with self._op_lock:
            self._jobs[job.job_id] = job

    def save_document_snapshot(self, snapshot: DocumentSnapshot) -> None:
        with self._op_lock:
            self._document_snapshots[(snapshot.workspace_id, snapshot.document_id)] = snapshot

    def get_document_snapshot(self, workspace_id: str, document_id: str) -> DocumentSnapshot | None:
        with self._op_lock:
            return self._document_snapshots.get((workspace_id, document_id))

    def list_document_snapshots(self, workspace_id: str) -> list[DocumentSnapshot]:
        with self._op_lock:
            return [snap for (ws_id, _), snap in self._document_snapshots.items() if ws_id == workspace_id]

    def save_benchmark_metadata(self, metadata: BenchmarkRunMetadata) -> None:
        with self._op_lock:
            self._benchmark_metadata[(metadata.workspace_id, metadata.benchmark_run_id)] = metadata

    def get_benchmark_metadata(self, workspace_id: str, benchmark_run_id: str) -> BenchmarkRunMetadata | None:
        with self._op_lock:
            return self._benchmark_metadata.get((workspace_id, benchmark_run_id))

    def list_benchmark_metadata(self, workspace_id: str) -> list[BenchmarkRunMetadata]:
        with self._op_lock:
            return [meta for (ws_id, _), meta in self._benchmark_metadata.items() if ws_id == workspace_id]


from functools import wraps
from typing import Callable, TypeVar, Any, cast

F = TypeVar("F", bound=Callable[..., Any])

def _handle_sqlite_errors(func: F) -> F:
    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            from src.schemas.contracts.boundary import StorageUnavailableError
            raise StorageUnavailableError(f"Database error: {str(e)}") from e
    return cast(F, wrapper)

def _translate_sqlite_errors_for_class(cls: type) -> type:
    for attr_name, attr_value in cls.__dict__.items():
        if callable(attr_value) and (not attr_name.startswith("__") or attr_name == "__init__"):
            setattr(cls, attr_name, _handle_sqlite_errors(attr_value))
    return cls

@_translate_sqlite_errors_for_class
class SQLiteWorkspaceBoundaryRepository:
    """Durable SQLite backed repository for workspaces, tombstones, and manifests."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            resolved_path = (
                os.getenv("KRE_WORKSPACE_SQLITE_PATH")
                or getattr(settings, "WORKSPACE_SQLITE_PATH", None)
                or getattr(settings, "TABLE_STORE_SQLITE_PATH", None)
                or str(Path(__file__).resolve().parent.parent.parent / "data" / "workspace_boundary.db")
            )
        else:
            resolved_path = str(db_path)

        self._db_path = resolved_path
        if self._db_path != ":memory:":
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL;")
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._ensure_tables()

    @property
    def db_path(self) -> str:
        return self._db_path

    def _ensure_tables(self) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS workspaces (
                        workspace_id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        owner_principal_id TEXT NOT NULL,
                        manifest_generation INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tombstones (
                        workspace_id TEXT NOT NULL,
                        resource_type TEXT NOT NULL,
                        resource_id TEXT NOT NULL,
                        manifest_generation INTEGER NOT NULL,
                        tombstoned_at TEXT NOT NULL,
                        deleted_by_principal_id TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, resource_type, resource_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS sources (
                        workspace_id TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, source_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS snapshots (
                        workspace_id TEXT NOT NULL,
                        snapshot_id TEXT NOT NULL,
                        manifest_generation INTEGER NOT NULL,
                        status TEXT NOT NULL,
                        source_versions_json TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, snapshot_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS manifests (
                        workspace_id TEXT PRIMARY KEY,
                        manifest_generation INTEGER NOT NULL,
                        active_source_ids_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        workspace_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        target_source_id TEXT,
                        target_source_version INTEGER,
                        status TEXT NOT NULL,
                        is_trusted_system_job INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS document_snapshots (
                        workspace_id TEXT NOT NULL,
                        document_id TEXT NOT NULL,
                        source TEXT NOT NULL,
                        version TEXT NOT NULL,
                        format TEXT NOT NULL,
                        incomplete_coverage_flag INTEGER NOT NULL,
                        exclusions_json TEXT NOT NULL,
                        persisted_rows INTEGER NOT NULL,
                        total_rows INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, document_id)
                    )
                    """
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS benchmark_runs (
                        workspace_id TEXT NOT NULL,
                        benchmark_run_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        total_queries INTEGER NOT NULL,
                        scored_queries INTEGER NOT NULL,
                        correctness_score REAL NOT NULL,
                        evidence_support_score REAL NOT NULL,
                        coverage_score REAL NOT NULL,
                        prompt_tokens INTEGER NOT NULL,
                        completion_tokens INTEGER NOT NULL,
                        total_tokens INTEGER NOT NULL,
                        avg_latency_ms REAL NOT NULL,
                        p95_latency_ms REAL NOT NULL,
                        ingestion_time_ms REAL NOT NULL,
                        s3_artifact_key TEXT NOT NULL,
                        PRIMARY KEY (workspace_id, benchmark_run_id)
                    )
                    """
                )

                # Seed fixtures only if workspaces table is empty
                cursor = self._conn.execute("SELECT count(*) as count FROM workspaces")
                if cursor.fetchone()["count"] == 0:
                    self._seed_fixtures_locked()

    def _seed_fixtures_locked(self) -> None:
        now = "2026-10-08T00:00:00Z"
        self._conn.execute(
            """
            INSERT OR REPLACE INTO workspaces (workspace_id, name, owner_principal_id, manifest_generation, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("ws_alpha", "Workspace Alpha", "usr_alice", 1, "active", now, now),
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO manifests (workspace_id, manifest_generation, active_source_ids_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("ws_alpha", 1, json.dumps([]), now),
        )

        self._conn.execute(
            """
            INSERT OR REPLACE INTO workspaces (workspace_id, name, owner_principal_id, manifest_generation, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("ws_beta", "Workspace Beta", "usr_bob", 1, "active", now, now),
        )
        self._conn.execute(
            """
            INSERT OR REPLACE INTO manifests (workspace_id, manifest_generation, active_source_ids_json, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("ws_beta", 1, json.dumps([]), now),
        )

    def reset_fixtures(self) -> None:
        """Reset repository state to baseline test fixtures."""
        with self._lock:
            with self._conn:
                self._conn.execute("DELETE FROM workspaces")
                self._conn.execute("DELETE FROM tombstones")
                self._conn.execute("DELETE FROM sources")
                self._conn.execute("DELETE FROM snapshots")
                self._conn.execute("DELETE FROM manifests")
                self._conn.execute("DELETE FROM jobs")
                self._conn.execute("DELETE FROM document_snapshots")
                self._conn.execute("DELETE FROM benchmark_runs")
                self._seed_fixtures_locked()

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return WorkspaceRecord(
                workspace_id=row["workspace_id"],
                name=row["name"],
                owner_principal_id=row["owner_principal_id"],
                manifest_generation=row["manifest_generation"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def create_workspace(
        self,
        name: str,
        owner_principal_id: str,
        workspace_id: str | None = None,
    ) -> WorkspaceRecord:
        with self._lock:
            ws_id = workspace_id or f"ws_{uuid.uuid4().hex[:12]}"
            now = _now_utc()
            with self._conn:
                self._conn.execute(
                    """
                    INSERT INTO workspaces (workspace_id, name, owner_principal_id, manifest_generation, status, created_at, updated_at)
                    VALUES (?, ?, ?, 1, 'active', ?, ?)
                    """,
                    (ws_id, name, owner_principal_id, now, now),
                )
                self._conn.execute(
                    """
                    INSERT INTO manifests (workspace_id, manifest_generation, active_source_ids_json, updated_at)
                    VALUES (?, 1, ?, ?)
                    """,
                    (ws_id, json.dumps([]), now),
                )
            return WorkspaceRecord(
                workspace_id=ws_id,
                name=name,
                owner_principal_id=owner_principal_id,
                manifest_generation=1,
                status="active",
                created_at=now,
                updated_at=now,
            )

    def tombstone_workspace(
        self,
        workspace_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT manifest_generation, status, owner_principal_id FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise FileNotFoundError(f"Workspace {workspace_id} not found")
                if row["owner_principal_id"] != caller_principal_id:
                    raise WorkspaceAccessDeniedError("Cannot delete foreign workspace")

                now = _now_utc()
                next_gen = row["manifest_generation"] + 1
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO tombstones (workspace_id, resource_type, resource_id, manifest_generation, tombstoned_at, deleted_by_principal_id, reason)
                    VALUES (?, 'workspace', ?, ?, ?, ?, ?)
                    """,
                    (workspace_id, workspace_id, next_gen, now, caller_principal_id, reason),
                )
                self._conn.execute(
                    "UPDATE workspaces SET status = 'tombstoned', manifest_generation = ?, updated_at = ? WHERE workspace_id = ?",
                    (next_gen, now, workspace_id),
                )
                return TombstoneRecord(
                    workspace_id=workspace_id,
                    resource_type="workspace",
                    resource_id=workspace_id,
                    manifest_generation=next_gen,
                    tombstoned_at=now,
                    deleted_by_principal_id=caller_principal_id,
                    reason=reason,
                )

    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT 1 FROM tombstones WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?",
                (workspace_id, resource_type, resource_id),
            )
            return cursor.fetchone() is not None

    def get_tombstone(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
    ) -> TombstoneRecord | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM tombstones WHERE workspace_id = ? AND resource_type = ? AND resource_id = ?",
                (workspace_id, resource_type, resource_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return TombstoneRecord(
                workspace_id=row["workspace_id"],
                resource_type=row["resource_type"],
                resource_id=row["resource_id"],
                manifest_generation=row["manifest_generation"],
                tombstoned_at=row["tombstoned_at"],
                deleted_by_principal_id=row["deleted_by_principal_id"],
                reason=row["reason"],
            )

    def record_source_tombstone(
        self,
        workspace_id: str,
        source_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT manifest_generation, status FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                )
                ws_row = cursor.fetchone()
                if not ws_row or ws_row["status"] == "tombstoned":
                    raise FileNotFoundError(f"Workspace {workspace_id} not found")

                cursor = self._conn.execute(
                    "SELECT * FROM tombstones WHERE workspace_id = ? AND resource_type = 'source' AND resource_id = ?",
                    (workspace_id, source_id),
                )
                existing = cursor.fetchone()
                if existing:
                    return TombstoneRecord(
                        workspace_id=existing["workspace_id"],
                        resource_type="source",
                        resource_id=existing["resource_id"],
                        manifest_generation=existing["manifest_generation"],
                        tombstoned_at=existing["tombstoned_at"],
                        deleted_by_principal_id=existing["deleted_by_principal_id"],
                        reason=existing["reason"],
                    )

                now = _now_utc()
                next_gen = ws_row["manifest_generation"] + 1
                self._conn.execute(
                    """
                    INSERT INTO tombstones (workspace_id, resource_type, resource_id, manifest_generation, tombstoned_at, deleted_by_principal_id, reason)
                    VALUES (?, 'source', ?, ?, ?, ?, ?)
                    """,
                    (workspace_id, source_id, next_gen, now, caller_principal_id, reason),
                )
                self._conn.execute(
                    "UPDATE sources SET status = 'tombstoned', updated_at = ? WHERE workspace_id = ? AND source_id = ?",
                    (now, workspace_id, source_id),
                )
                self._conn.execute(
                    "UPDATE workspaces SET manifest_generation = ?, updated_at = ? WHERE workspace_id = ?",
                    (next_gen, now, workspace_id),
                )
                return TombstoneRecord(
                    workspace_id=workspace_id,
                    resource_type="source",
                    resource_id=source_id,
                    manifest_generation=next_gen,
                    tombstoned_at=now,
                    deleted_by_principal_id=caller_principal_id,
                    reason=reason,
                )

    def get_source(self, workspace_id: str, source_id: str) -> SourceItem | None:
        with self._lock:
            if self.has_tombstone(workspace_id, "source", source_id):
                return None
            cursor = self._conn.execute(
                "SELECT * FROM sources WHERE workspace_id = ? AND source_id = ?",
                (workspace_id, source_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return SourceItem(
                source_id=row["source_id"],
                title=row["title"],
                status=row["status"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def list_sources(self, workspace_id: str) -> list[SourceItem]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT s.* FROM sources s
                LEFT JOIN tombstones t ON s.workspace_id = t.workspace_id AND t.resource_type = 'source' AND s.source_id = t.resource_id
                WHERE s.workspace_id = ? AND t.resource_id IS NULL
                ORDER BY s.source_id ASC
                """,
                (workspace_id,),
            )
            results = []
            for row in cursor.fetchall():
                results.append(
                    SourceItem(
                        source_id=row["source_id"],
                        title=row["title"],
                        status=row["status"],
                        created_at=row["created_at"],
                        updated_at=row["updated_at"],
                    )
                )
            return results

    def create_source(
        self,
        workspace_id: str,
        title: str,
        source_id: str | None = None,
    ) -> SourceItem:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT status FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                )
                ws = cursor.fetchone()
                if not ws or ws["status"] == "tombstoned":
                    raise FileNotFoundError(f"Workspace {workspace_id} not found")

                src_id = source_id or f"src_{uuid.uuid4().hex[:12]}"
                now = _now_utc()
                self._conn.execute(
                    """
                    INSERT INTO sources (workspace_id, source_id, title, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'registered', ?, ?)
                    """,
                    (workspace_id, src_id, title, now, now),
                )
                return SourceItem(
                    source_id=src_id,
                    title=title,
                    status="registered",
                    created_at=now,
                    updated_at=now,
                )

    def get_snapshot(self, workspace_id: str, snapshot_id: str) -> SnapshotRecord | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM snapshots WHERE workspace_id = ? AND snapshot_id = ?",
                (workspace_id, snapshot_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            source_versions_raw = json.loads(row["source_versions_json"])
            source_versions = tuple((s[0], s[1]) for s in source_versions_raw)
            artifact_ids = tuple(json.loads(row["artifact_ids_json"]))
            return SnapshotRecord(
                snapshot_id=row["snapshot_id"],
                workspace_id=row["workspace_id"],
                manifest_generation=row["manifest_generation"],
                source_versions=source_versions,
                artifact_ids=artifact_ids,
                status=row["status"],
                created_at=row["created_at"],
            )

    def save_snapshot(self, snapshot: SnapshotRecord) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO snapshots (workspace_id, snapshot_id, manifest_generation, status, source_versions_json, artifact_ids_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.workspace_id,
                        snapshot.snapshot_id,
                        snapshot.manifest_generation,
                        snapshot.status,
                        json.dumps([list(pair) for pair in snapshot.source_versions]),
                        json.dumps(list(snapshot.artifact_ids)),
                        snapshot.created_at,
                    ),
                )

    def get_manifest(self, workspace_id: str) -> PublicationManifestRecord | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM manifests WHERE workspace_id = ?",
                (workspace_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return PublicationManifestRecord(
                workspace_id=row["workspace_id"],
                manifest_generation=row["manifest_generation"],
                active_source_ids=tuple(json.loads(row["active_source_ids_json"])),
                updated_at=row["updated_at"],
            )

    def cas_update_manifest(
        self,
        workspace_id: str,
        expected_generation: int,
        delta: dict[str, Any],
    ) -> PublicationManifestRecord:
        with self._lock:
            with self._conn:
                cursor = self._conn.execute(
                    "SELECT manifest_generation, status FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                )
                ws = cursor.fetchone()
                if not ws or ws["status"] == "tombstoned":
                    raise FileNotFoundError(f"Workspace {workspace_id} not found")

                if ws["manifest_generation"] != expected_generation:
                    raise CASConflictError(
                        f"CAS conflict on workspace {workspace_id}: expected generation {expected_generation}, "
                        f"authoritative generation is {ws['manifest_generation']}"
                    )

                next_gen = ws["manifest_generation"] + 1
                now = _now_utc()
                self._conn.execute(
                    "UPDATE workspaces SET manifest_generation = ?, updated_at = ? WHERE workspace_id = ?",
                    (next_gen, now, workspace_id),
                )
                new_sources = delta.get("active_source_ids", ())
                self._conn.execute(
                    """
                    INSERT INTO manifests (workspace_id, manifest_generation, active_source_ids_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(workspace_id) DO UPDATE SET
                        manifest_generation = excluded.manifest_generation,
                        active_source_ids_json = excluded.active_source_ids_json,
                        updated_at = excluded.updated_at
                    """,
                    (workspace_id, next_gen, json.dumps(list(new_sources)), now),
                )
                return PublicationManifestRecord(
                    workspace_id=workspace_id,
                    manifest_generation=next_gen,
                    active_source_ids=tuple(new_sources),
                    updated_at=now,
                )

    def get_job(self, job_id: str) -> JobRecord | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return JobRecord(
                job_id=row["job_id"],
                workspace_id=row["workspace_id"],
                operation=row["operation"],
                target_source_id=row["target_source_id"],
                target_source_version=row["target_source_version"],
                status=row["status"],
                is_trusted_system_job=bool(row["is_trusted_system_job"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def save_job(self, job: JobRecord) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO jobs (job_id, workspace_id, operation, target_source_id, target_source_version, status, is_trusted_system_job, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.job_id,
                        job.workspace_id,
                        job.operation,
                        job.target_source_id,
                        job.target_source_version,
                        job.status,
                        1 if job.is_trusted_system_job else 0,
                        job.created_at,
                        job.updated_at,
                    ),
                )

    def save_document_snapshot(self, snapshot: DocumentSnapshot) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    """
                    INSERT OR REPLACE INTO document_snapshots (
                        workspace_id, document_id, source, version, format,
                        incomplete_coverage_flag, exclusions_json, persisted_rows, total_rows, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.workspace_id,
                        snapshot.document_id,
                        snapshot.source,
                        snapshot.version,
                        snapshot.format,
                        1 if snapshot.incomplete_coverage_flag else 0,
                        json.dumps(list(snapshot.exclusions)),
                        snapshot.persisted_rows,
                        snapshot.total_rows,
                        snapshot.created_at,
                    ),
                )

    def get_document_snapshot(self, workspace_id: str, document_id: str) -> DocumentSnapshot | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM document_snapshots WHERE workspace_id = ? AND document_id = ?",
                (workspace_id, document_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return DocumentSnapshot(
                workspace_id=row["workspace_id"],
                document_id=row["document_id"],
                source=row["source"],
                version=row["version"],
                format=row["format"],
                incomplete_coverage_flag=bool(row["incomplete_coverage_flag"]),
                exclusions=tuple(json.loads(row["exclusions_json"])),
                persisted_rows=int(row["persisted_rows"]),
                total_rows=int(row["total_rows"]),
                created_at=row["created_at"],
            )

    def list_document_snapshots(self, workspace_id: str) -> list[DocumentSnapshot]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM document_snapshots WHERE workspace_id = ? ORDER BY created_at ASC",
                (workspace_id,),
            )
            rows = cursor.fetchall()
            return [
                DocumentSnapshot(
                    workspace_id=r["workspace_id"],
                    document_id=r["document_id"],
                    source=r["source"],
                    version=r["version"],
                    format=r["format"],
                    incomplete_coverage_flag=bool(r["incomplete_coverage_flag"]),
                    exclusions=tuple(json.loads(r["exclusions_json"])),
                    persisted_rows=int(r["persisted_rows"]),
                    total_rows=int(r["total_rows"]),
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def save_benchmark_metadata(self, metadata: BenchmarkRunMetadata) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO benchmark_runs (
                    workspace_id, benchmark_run_id, status, created_at,
                    total_queries, scored_queries, correctness_score,
                    evidence_support_score, coverage_score, prompt_tokens,
                    completion_tokens, total_tokens, avg_latency_ms,
                    p95_latency_ms, ingestion_time_ms, s3_artifact_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.workspace_id,
                    metadata.benchmark_run_id,
                    metadata.status,
                    metadata.created_at,
                    metadata.total_queries,
                    metadata.scored_queries,
                    metadata.correctness_score,
                    metadata.evidence_support_score,
                    metadata.coverage_score,
                    metadata.prompt_tokens,
                    metadata.completion_tokens,
                    metadata.total_tokens,
                    metadata.avg_latency_ms,
                    metadata.p95_latency_ms,
                    metadata.ingestion_time_ms,
                    metadata.s3_artifact_key,
                ),
            )
            self._conn.commit()

    def get_benchmark_metadata(self, workspace_id: str, benchmark_run_id: str) -> BenchmarkRunMetadata | None:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM benchmark_runs WHERE workspace_id = ? AND benchmark_run_id = ?",
                (workspace_id, benchmark_run_id),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return BenchmarkRunMetadata(
                workspace_id=row["workspace_id"],
                benchmark_run_id=row["benchmark_run_id"],
                status=row["status"],
                created_at=row["created_at"],
                total_queries=row["total_queries"],
                scored_queries=row["scored_queries"],
                correctness_score=float(row["correctness_score"]),
                evidence_support_score=float(row["evidence_support_score"]),
                coverage_score=float(row["coverage_score"]),
                prompt_tokens=int(row["prompt_tokens"]),
                completion_tokens=int(row["completion_tokens"]),
                total_tokens=int(row["total_tokens"]),
                avg_latency_ms=float(row["avg_latency_ms"]),
                p95_latency_ms=float(row["p95_latency_ms"]),
                ingestion_time_ms=float(row["ingestion_time_ms"]),
                s3_artifact_key=row["s3_artifact_key"],
            )

    def list_benchmark_metadata(self, workspace_id: str) -> list[BenchmarkRunMetadata]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT * FROM benchmark_runs WHERE workspace_id = ? ORDER BY created_at ASC",
                (workspace_id,),
            )
            rows = cursor.fetchall()
            return [
                BenchmarkRunMetadata(
                    workspace_id=r["workspace_id"],
                    benchmark_run_id=r["benchmark_run_id"],
                    status=r["status"],
                    created_at=r["created_at"],
                    total_queries=r["total_queries"],
                    scored_queries=r["scored_queries"],
                    correctness_score=float(r["correctness_score"]),
                    evidence_support_score=float(r["evidence_support_score"]),
                    coverage_score=float(r["coverage_score"]),
                    prompt_tokens=int(r["prompt_tokens"]),
                    completion_tokens=int(r["completion_tokens"]),
                    total_tokens=int(r["total_tokens"]),
                    avg_latency_ms=float(r["avg_latency_ms"]),
                    p95_latency_ms=float(r["p95_latency_ms"]),
                    ingestion_time_ms=float(r["ingestion_time_ms"]),
                    s3_artifact_key=r["s3_artifact_key"],
                )
                for r in rows
            ]



class DynamoDBWorkspaceBoundaryRepository:
    """Durable DynamoDB backed repository for workspace boundaries, generation fencing, and tombstones."""

    def __init__(
        self,
        table_name: str | None = None,
        dynamodb_resource: Any | None = None,
        dynamodb_client: Any | None = None,
    ) -> None:
        self._table_name = table_name or settings.DYNAMODB_TABLE_NAME
        self._resource = dynamodb_resource
        self._client = dynamodb_client
        self._table = None
        self._lock = threading.RLock()

    @property
    def client(self) -> Any:
        if self._client is None:
            from src.aws.infra import get_client
            self._client = get_client("dynamodb")
        return self._client

    @property
    def resource(self) -> Any:
        if self._resource is None:
            from src.aws.infra import get_resource
            self._resource = get_resource("dynamodb")
        return self._resource

    @property
    def table(self) -> Any:
        if self._table is None:
            self._table = self.resource.Table(self._table_name)
        return self._table

    def reset_fixtures(self) -> None:
        """Seed baseline test fixtures into DynamoDB."""
        self.create_workspace("Workspace Alpha", "usr_alice", workspace_id="ws_alpha")
        self.create_workspace("Workspace Beta", "usr_bob", workspace_id="ws_beta")

    def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": "BOUNDARY_META"}
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting workspace {workspace_id}: {err}") from err

        item = resp.get("Item")
        if not item:
            return None
        return WorkspaceRecord(
            workspace_id=item["workspace_id"],
            name=item["name"],
            owner_principal_id=item["owner_principal_id"],
            manifest_generation=int(item["manifest_generation"]),
            status=item["status"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
        )

    def create_workspace(
        self,
        name: str,
        owner_principal_id: str,
        workspace_id: str | None = None,
    ) -> WorkspaceRecord:
        ws_id = workspace_id or f"ws_{uuid.uuid4().hex[:12]}"
        now = _now_utc()
        record = WorkspaceRecord(
            workspace_id=ws_id,
            name=name,
            owner_principal_id=owner_principal_id,
            manifest_generation=1,
            status="active",
            created_at=now,
            updated_at=now,
        )
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{ws_id}",
                    "SK": "BOUNDARY_META",
                    "workspace_id": ws_id,
                    "name": name,
                    "owner_principal_id": owner_principal_id,
                    "manifest_generation": 1,
                    "status": "active",
                    "created_at": now,
                    "updated_at": now,
                }
            )
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{ws_id}",
                    "SK": "BOUNDARY_MANIFEST",
                    "workspace_id": ws_id,
                    "manifest_generation": 1,
                    "active_source_ids_json": "[]",
                    "updated_at": now,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error creating workspace {ws_id}: {err}") from err
        return record

    def tombstone_workspace(
        self,
        workspace_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        ws = self.get_workspace(workspace_id)
        if not ws:
            raise FileNotFoundError(f"Workspace {workspace_id} not found")
        if ws.owner_principal_id != caller_principal_id:
            raise WorkspaceAccessDeniedError("Cannot delete foreign workspace")

        now = _now_utc()
        next_gen = ws.manifest_generation + 1
        tombstone = TombstoneRecord(
            workspace_id=workspace_id,
            resource_type="workspace",
            resource_id=workspace_id,
            manifest_generation=next_gen,
            tombstoned_at=now,
            deleted_by_principal_id=caller_principal_id,
            reason=reason,
        )
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"TOMBSTONE#workspace#{workspace_id}",
                    "workspace_id": workspace_id,
                    "resource_type": "workspace",
                    "resource_id": workspace_id,
                    "manifest_generation": next_gen,
                    "tombstoned_at": now,
                    "deleted_by_principal_id": caller_principal_id,
                    "reason": reason,
                }
            )
            self.table.update_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": "BOUNDARY_META"},
                UpdateExpression="SET #st = :tomb, manifest_generation = :gen, updated_at = :upd",
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={
                    ":tomb": "tombstoned",
                    ":gen": next_gen,
                    ":upd": now,
                },
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error tombstoning workspace {workspace_id}: {err}") from err
        return tombstone

    def has_tombstone(self, workspace_id: str, resource_type: str, resource_id: str) -> bool:
        try:
            resp = self.table.get_item(
                Key={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"TOMBSTONE#{resource_type}#{resource_id}",
                }
            )
            return "Item" in resp
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error checking tombstone: {err}") from err

    def get_tombstone(
        self,
        workspace_id: str,
        resource_type: str,
        resource_id: str,
    ) -> TombstoneRecord | None:
        try:
            resp = self.table.get_item(
                Key={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"TOMBSTONE#{resource_type}#{resource_id}",
                }
            )
            item = resp.get("Item")
            if not item:
                return None
            return TombstoneRecord(
                workspace_id=item["workspace_id"],
                resource_type=item["resource_type"],
                resource_id=item["resource_id"],
                manifest_generation=int(item["manifest_generation"]),
                tombstoned_at=item["tombstoned_at"],
                deleted_by_principal_id=item["deleted_by_principal_id"],
                reason=item["reason"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting tombstone: {err}") from err

    def record_source_tombstone(
        self,
        workspace_id: str,
        source_id: str,
        caller_principal_id: str,
        reason: str = "user_requested",
    ) -> TombstoneRecord:
        ws = self.get_workspace(workspace_id)
        if not ws or ws.status == "tombstoned":
            raise FileNotFoundError(f"Workspace {workspace_id} not found")

        existing = self.get_tombstone(workspace_id, "source", source_id)
        if existing:
            return existing

        now = _now_utc()
        next_gen = ws.manifest_generation + 1
        tombstone = TombstoneRecord(
            workspace_id=workspace_id,
            resource_type="source",
            resource_id=source_id,
            manifest_generation=next_gen,
            tombstoned_at=now,
            deleted_by_principal_id=caller_principal_id,
            reason=reason,
        )
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"TOMBSTONE#source#{source_id}",
                    "workspace_id": workspace_id,
                    "resource_type": "source",
                    "resource_id": source_id,
                    "manifest_generation": next_gen,
                    "tombstoned_at": now,
                    "deleted_by_principal_id": caller_principal_id,
                    "reason": reason,
                }
            )
            try:
                self.table.update_item(
                    Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"BOUNDARY_SOURCE#{source_id}"},
                    UpdateExpression="SET #st = :tomb, updated_at = :upd",
                    ExpressionAttributeNames={"#st": "status"},
                    ExpressionAttributeValues={":tomb": "tombstoned", ":upd": now},
                )
            except Exception:
                pass
            self.table.update_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": "BOUNDARY_META"},
                UpdateExpression="SET manifest_generation = :gen, updated_at = :upd",
                ExpressionAttributeValues={":gen": next_gen, ":upd": now},
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error recording source tombstone: {err}") from err
        return tombstone

    def get_source(self, workspace_id: str, source_id: str) -> SourceItem | None:
        if self.has_tombstone(workspace_id, "source", source_id):
            return None
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"BOUNDARY_SOURCE#{source_id}"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return SourceItem(
                source_id=item["source_id"],
                title=item["title"],
                status=item["status"],
                created_at=item["created_at"],
                updated_at=item["updated_at"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting source: {err}") from err

    def list_sources(self, workspace_id: str) -> list[SourceItem]:
        from boto3.dynamodb.conditions import Key
        try:
            resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}") & Key("SK").begins_with("BOUNDARY_SOURCE#")
            )
            items = []
            for item in resp.get("Items", []):
                s_id = item["source_id"]
                if self.has_tombstone(workspace_id, "source", s_id) or item["status"] == "tombstoned":
                    continue
                items.append(
                    SourceItem(
                        source_id=s_id,
                        title=item["title"],
                        status=item["status"],
                        created_at=item["created_at"],
                        updated_at=item["updated_at"],
                    )
                )
            return items
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error listing sources: {err}") from err

    def create_source(
        self,
        workspace_id: str,
        title: str,
        source_id: str | None = None,
    ) -> SourceItem:
        ws = self.get_workspace(workspace_id)
        if not ws or ws.status == "tombstoned":
            raise FileNotFoundError(f"Workspace {workspace_id} not found")

        s_id = source_id or f"src_{uuid.uuid4().hex[:12]}"
        now = _now_utc()
        item = SourceItem(
            source_id=s_id,
            title=title,
            status="registered",
            created_at=now,
            updated_at=now,
        )
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{workspace_id}",
                    "SK": f"BOUNDARY_SOURCE#{s_id}",
                    "workspace_id": workspace_id,
                    "source_id": s_id,
                    "title": title,
                    "status": "registered",
                    "created_at": now,
                    "updated_at": now,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error creating source: {err}") from err
        return item

    def get_snapshot(self, workspace_id: str, snapshot_id: str) -> SnapshotRecord | None:
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"BOUNDARY_SNAPSHOT#{snapshot_id}"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return SnapshotRecord(
                snapshot_id=item["snapshot_id"],
                workspace_id=item["workspace_id"],
                manifest_generation=int(item["manifest_generation"]),
                status=item["status"],
                source_versions=tuple(tuple(x) for x in json.loads(item["source_versions_json"])),
                artifact_ids=tuple(json.loads(item["artifact_ids_json"])),
                created_at=item["created_at"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting snapshot: {err}") from err

    def save_snapshot(self, snapshot: SnapshotRecord) -> None:
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{snapshot.workspace_id}",
                    "SK": f"BOUNDARY_SNAPSHOT#{snapshot.snapshot_id}",
                    "workspace_id": snapshot.workspace_id,
                    "snapshot_id": snapshot.snapshot_id,
                    "manifest_generation": snapshot.manifest_generation,
                    "status": snapshot.status,
                    "source_versions_json": json.dumps(snapshot.source_versions),
                    "artifact_ids_json": json.dumps(list(snapshot.artifact_ids)),
                    "created_at": snapshot.created_at,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error saving snapshot: {err}") from err

    def get_manifest(self, workspace_id: str) -> PublicationManifestRecord | None:
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": "BOUNDARY_MANIFEST"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return PublicationManifestRecord(
                workspace_id=item["workspace_id"],
                manifest_generation=int(item["manifest_generation"]),
                active_source_ids=tuple(json.loads(item["active_source_ids_json"])),
                updated_at=item["updated_at"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting manifest: {err}") from err

    def cas_update_manifest(
        self,
        workspace_id: str,
        expected_generation: int,
        delta: dict[str, Any],
    ) -> PublicationManifestRecord:
        manifest = self.get_manifest(workspace_id)
        if not manifest:
            raise FileNotFoundError(f"Manifest for workspace {workspace_id} not found")

        if manifest.manifest_generation != expected_generation:
            raise CASConflictError(
                f"CAS conflict on workspace {workspace_id}: expected generation {expected_generation}, got {manifest.manifest_generation}"
            )

        active_set = set(manifest.active_source_ids)
        for s_id in delta.get("add_source_ids", ()):
            if self.has_tombstone(workspace_id, "source", s_id):
                continue
            active_set.add(s_id)
        for s_id in delta.get("remove_source_ids", ()):
            active_set.discard(s_id)

        next_gen = expected_generation + 1
        now = _now_utc()
        new_active = tuple(sorted(active_set))

        try:
            self.table.update_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": "BOUNDARY_MANIFEST"},
                UpdateExpression="SET manifest_generation = :next_gen, active_source_ids_json = :sources, updated_at = :upd",
                ConditionExpression="manifest_generation = :exp",
                ExpressionAttributeValues={
                    ":next_gen": next_gen,
                    ":sources": json.dumps(list(new_active)),
                    ":upd": now,
                    ":exp": expected_generation,
                },
            )
        except Exception as err:
            err_code = getattr(err, "response", {}).get("Error", {}).get("Code", "")
            if err_code == "ConditionalCheckFailedException" or "ConditionalCheckFailed" in str(err):
                raise CASConflictError(
                    f"CAS conflict on workspace {workspace_id}: expected generation {expected_generation}"
                ) from err
            raise StorageUnavailableError(f"DynamoDB error updating manifest: {err}") from err

        return PublicationManifestRecord(
            workspace_id=workspace_id,
            manifest_generation=next_gen,
            active_source_ids=new_active,
            updated_at=now,
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        try:
            resp = self.table.get_item(
                Key={"PK": "GLOBAL_JOB", "SK": f"JOB#{job_id}"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return JobRecord(
                job_id=item["job_id"],
                workspace_id=item["workspace_id"],
                operation=item["operation"],
                target_source_id=item.get("target_source_id"),
                target_source_version=item.get("target_source_version"),
                status=item["status"],
                is_trusted_system_job=bool(item.get("is_trusted_system_job", True)),
                created_at=item["created_at"],
                updated_at=item["updated_at"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting job: {err}") from err

    def save_job(self, job: JobRecord) -> None:
        try:
            self.table.put_item(
                Item={
                    "PK": "GLOBAL_JOB",
                    "SK": f"JOB#{job.job_id}",
                    "job_id": job.job_id,
                    "workspace_id": job.workspace_id,
                    "operation": job.operation,
                    "target_source_id": job.target_source_id,
                    "target_source_version": job.target_source_version,
                    "status": job.status,
                    "is_trusted_system_job": job.is_trusted_system_job,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error saving job: {err}") from err

    def save_document_snapshot(self, snapshot: DocumentSnapshot) -> None:
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{snapshot.workspace_id}",
                    "SK": f"DOC_SNAPSHOT#{snapshot.document_id}",
                    "workspace_id": snapshot.workspace_id,
                    "document_id": snapshot.document_id,
                    "source": snapshot.source,
                    "version": snapshot.version,
                    "format": snapshot.format,
                    "incomplete_coverage_flag": snapshot.incomplete_coverage_flag,
                    "exclusions_json": json.dumps(list(snapshot.exclusions)),
                    "persisted_rows": snapshot.persisted_rows,
                    "total_rows": snapshot.total_rows,
                    "created_at": snapshot.created_at,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error saving document snapshot: {err}") from err

    def get_document_snapshot(self, workspace_id: str, document_id: str) -> DocumentSnapshot | None:
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"DOC_SNAPSHOT#{document_id}"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return DocumentSnapshot(
                workspace_id=item["workspace_id"],
                document_id=item["document_id"],
                source=item["source"],
                version=item["version"],
                format=item["format"],
                incomplete_coverage_flag=bool(item.get("incomplete_coverage_flag", False)),
                exclusions=tuple(json.loads(item.get("exclusions_json", "[]"))),
                persisted_rows=int(item.get("persisted_rows", 0)),
                total_rows=int(item.get("total_rows", 0)),
                created_at=item["created_at"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting document snapshot: {err}") from err

    def list_document_snapshots(self, workspace_id: str) -> list[DocumentSnapshot]:
        try:
            from boto3.dynamodb.conditions import Key
            resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                & Key("SK").begins_with("DOC_SNAPSHOT#")
            )
            items = resp.get("Items", [])
            return [
                DocumentSnapshot(
                    workspace_id=item["workspace_id"],
                    document_id=item["document_id"],
                    source=item["source"],
                    version=item["version"],
                    format=item["format"],
                    incomplete_coverage_flag=bool(item.get("incomplete_coverage_flag", False)),
                    exclusions=tuple(json.loads(item.get("exclusions_json", "[]"))),
                    persisted_rows=int(item.get("persisted_rows", 0)),
                    total_rows=int(item.get("total_rows", 0)),
                    created_at=item["created_at"],
                )
                for item in items
            ]
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error listing document snapshots: {err}") from err

    def save_benchmark_metadata(self, metadata: BenchmarkRunMetadata) -> None:
        from decimal import Decimal
        try:
            self.table.put_item(
                Item={
                    "PK": f"WORKSPACE#{metadata.workspace_id}",
                    "SK": f"BENCHMARK#{metadata.benchmark_run_id}",
                    "workspace_id": metadata.workspace_id,
                    "benchmark_run_id": metadata.benchmark_run_id,
                    "status": metadata.status,
                    "created_at": metadata.created_at,
                    "total_queries": metadata.total_queries,
                    "scored_queries": metadata.scored_queries,
                    "correctness_score": Decimal(str(round(metadata.correctness_score, 4))),
                    "evidence_support_score": Decimal(str(round(metadata.evidence_support_score, 4))),
                    "coverage_score": Decimal(str(round(metadata.coverage_score, 4))),
                    "prompt_tokens": metadata.prompt_tokens,
                    "completion_tokens": metadata.completion_tokens,
                    "total_tokens": metadata.total_tokens,
                    "avg_latency_ms": Decimal(str(round(metadata.avg_latency_ms, 2))),
                    "p95_latency_ms": Decimal(str(round(metadata.p95_latency_ms, 2))),
                    "ingestion_time_ms": Decimal(str(round(metadata.ingestion_time_ms, 2))),
                    "s3_artifact_key": metadata.s3_artifact_key,
                }
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error saving benchmark metadata: {err}") from err

    def get_benchmark_metadata(self, workspace_id: str, benchmark_run_id: str) -> BenchmarkRunMetadata | None:
        try:
            resp = self.table.get_item(
                Key={"PK": f"WORKSPACE#{workspace_id}", "SK": f"BENCHMARK#{benchmark_run_id}"}
            )
            item = resp.get("Item")
            if not item:
                return None
            return BenchmarkRunMetadata(
                workspace_id=item["workspace_id"],
                benchmark_run_id=item["benchmark_run_id"],
                status=item["status"],
                created_at=item["created_at"],
                total_queries=int(item.get("total_queries", 0)),
                scored_queries=int(item.get("scored_queries", 0)),
                correctness_score=float(item.get("correctness_score", 0.0)),
                evidence_support_score=float(item.get("evidence_support_score", 0.0)),
                coverage_score=float(item.get("coverage_score", 0.0)),
                prompt_tokens=int(item.get("prompt_tokens", 0)),
                completion_tokens=int(item.get("completion_tokens", 0)),
                total_tokens=int(item.get("total_tokens", 0)),
                avg_latency_ms=float(item.get("avg_latency_ms", 0.0)),
                p95_latency_ms=float(item.get("p95_latency_ms", 0.0)),
                ingestion_time_ms=float(item.get("ingestion_time_ms", 0.0)),
                s3_artifact_key=item["s3_artifact_key"],
            )
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error getting benchmark metadata: {err}") from err

    def list_benchmark_metadata(self, workspace_id: str) -> list[BenchmarkRunMetadata]:
        try:
            from boto3.dynamodb.conditions import Key
            resp = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"WORKSPACE#{workspace_id}")
                & Key("SK").begins_with("BENCHMARK#")
            )
            items = resp.get("Items", [])
            return [
                BenchmarkRunMetadata(
                    workspace_id=item["workspace_id"],
                    benchmark_run_id=item["benchmark_run_id"],
                    status=item["status"],
                    created_at=item["created_at"],
                    total_queries=int(item.get("total_queries", 0)),
                    scored_queries=int(item.get("scored_queries", 0)),
                    correctness_score=float(item.get("correctness_score", 0.0)),
                    evidence_support_score=float(item.get("evidence_support_score", 0.0)),
                    coverage_score=float(item.get("coverage_score", 0.0)),
                    prompt_tokens=int(item.get("prompt_tokens", 0)),
                    completion_tokens=int(item.get("completion_tokens", 0)),
                    total_tokens=int(item.get("total_tokens", 0)),
                    avg_latency_ms=float(item.get("avg_latency_ms", 0.0)),
                    p95_latency_ms=float(item.get("p95_latency_ms", 0.0)),
                    ingestion_time_ms=float(item.get("ingestion_time_ms", 0.0)),
                    s3_artifact_key=item["s3_artifact_key"],
                )
                for item in items
            ]
        except Exception as err:
            raise StorageUnavailableError(f"DynamoDB error listing benchmark metadata: {err}") from err


_SHARED_BOUNDARY_REPO: WorkspaceBoundaryRepositoryBase | None = None


def get_workspace_boundary_repo(
    reset: bool = False,
    require_durable: bool = False,
) -> WorkspaceBoundaryRepositoryBase:
    """Return process scoped workspace boundary repository.

    Backend selection:
      1. Explicit environment variable: KRE_WORKSPACE_REPO_BACKEND in ('dynamodb', 'sqlite', 'memory').
      2. If require_durable=True or ENVIRONMENT == 'prod', 'memory' is strictly rejected.
      3. ENVIRONMENT == 'prod': DynamoDBWorkspaceBoundaryRepository with durable persistent storage.
      4. ENVIRONMENT == 'dev': Live DynamoDBWorkspaceBoundaryRepository if configured, else SQLiteWorkspaceBoundaryRepository.
      5. ENVIRONMENT == 'test': InMemoryWorkspaceBoundaryRepository (or SQLite/DynamoDB if explicitly selected).
    """
    global _SHARED_BOUNDARY_REPO
    if reset or _SHARED_BOUNDARY_REPO is None:
        backend_override = (
            os.getenv("KRE_WORKSPACE_REPO_BACKEND")
            or getattr(settings, "WORKSPACE_REPO_BACKEND", None)
            or ""
        ).lower().strip()

        env = (getattr(settings, "ENVIRONMENT", "") or os.getenv("ENVIRONMENT") or "").lower().strip()

        if backend_override == "dynamodb":
            _SHARED_BOUNDARY_REPO = DynamoDBWorkspaceBoundaryRepository()
        elif backend_override == "sqlite":
            _SHARED_BOUNDARY_REPO = SQLiteWorkspaceBoundaryRepository()
        elif backend_override == "memory":
            if require_durable or env in ("prod", "production"):
                raise RuntimeError("Memory repository cannot be used when durable backend is required or in production.")
            _SHARED_BOUNDARY_REPO = InMemoryWorkspaceBoundaryRepository()
        else:
            if env in ("prod", "production"):
                _SHARED_BOUNDARY_REPO = DynamoDBWorkspaceBoundaryRepository()
            elif require_durable or env == "dev":
                _SHARED_BOUNDARY_REPO = SQLiteWorkspaceBoundaryRepository()
            else:
                _SHARED_BOUNDARY_REPO = InMemoryWorkspaceBoundaryRepository()

    return _SHARED_BOUNDARY_REPO


class WorkspaceBoundaryRepositoryProxy:
    """Dynamic proxy that forwards all repository calls to get_workspace_boundary_repo."""

    def __getattr__(self, name: str) -> Any:
        repo = get_workspace_boundary_repo()
        return getattr(repo, name)


# Default proxy and aliases for backwards compatibility
workspace_boundary_repo = WorkspaceBoundaryRepositoryProxy()
WorkspaceBoundaryRepository = InMemoryWorkspaceBoundaryRepository

__all__ = [
    "WorkspaceBoundaryRepositoryBase",
    "InMemoryWorkspaceBoundaryRepository",
    "SQLiteWorkspaceBoundaryRepository",
    "DynamoDBWorkspaceBoundaryRepository",
    "WorkspaceBoundaryRepositoryProxy",
    "WorkspaceBoundaryRepository",
    "get_workspace_boundary_repo",
    "workspace_boundary_repo",
]

