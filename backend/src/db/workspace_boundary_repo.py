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
    CASConflictError,
    JobRecord,
    PublicationManifestRecord,
    SnapshotRecord,
    SourceItem,
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


class InMemoryWorkspaceBoundaryRepository:
    """Ephemeral in memory repository strictly for isolated test suites."""

    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._tombstones: dict[tuple[str, str, str], TombstoneRecord] = {}
        self._sources: dict[tuple[str, str], SourceItem] = {}
        self._snapshots: dict[tuple[str, str], SnapshotRecord] = {}
        self._manifests: dict[str, PublicationManifestRecord] = {}
        self._jobs: dict[str, JobRecord] = {}
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


_SHARED_BOUNDARY_REPO: WorkspaceBoundaryRepositoryBase | None = None


def get_workspace_boundary_repo(
    reset: bool = False,
    require_durable: bool = False,
) -> WorkspaceBoundaryRepositoryBase:
    """Return process scoped workspace boundary repository.

    Backend selection:
      1. Explicit environment variable: KRE_WORKSPACE_REPO_BACKEND in ('sqlite', 'memory').
      2. If require_durable=True or ENVIRONMENT == 'prod', 'memory' is strictly rejected.
      3. ENVIRONMENT == 'prod' or 'dev': SQLiteWorkspaceBoundaryRepository with durable persistent storage.
      4. ENVIRONMENT == 'test': InMemoryWorkspaceBoundaryRepository (or SQLite if explicitly selected).
    """
    global _SHARED_BOUNDARY_REPO
    if reset or _SHARED_BOUNDARY_REPO is None:
        backend_override = (
            os.getenv("KRE_WORKSPACE_REPO_BACKEND")
            or getattr(settings, "WORKSPACE_REPO_BACKEND", None)
            or ""
        ).lower().strip()

        env = (settings.ENVIRONMENT or "").lower()

        if backend_override == "memory":
            if require_durable or env in ("prod", "production"):
                raise RuntimeError("Memory repository cannot be used when durable backend is required or in production.")
            _SHARED_BOUNDARY_REPO = InMemoryWorkspaceBoundaryRepository()
        elif backend_override == "sqlite":
            _SHARED_BOUNDARY_REPO = SQLiteWorkspaceBoundaryRepository()
        else:
            if require_durable or env in ("prod", "production", "dev"):
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
    "WorkspaceBoundaryRepositoryProxy",
    "WorkspaceBoundaryRepository",
    "get_workspace_boundary_repo",
    "workspace_boundary_repo",
]
