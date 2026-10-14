"""Recoverable benchmark persistence coordinator.

Enforces the AC-10 recoverable write sequence:
1. Detailed versioned JSON artifacts are written to S3 first.
2. Only after S3 write confirmation is compact metadata persisted to DynamoDB.
Missing S3 objects for committed DynamoDB records are prohibited.
Orphan S3 objects are acceptable if DynamoDB write encounters a transient error.
"""

import logging
from typing import Any

from src.schemas.contracts.boundary import BenchmarkRunMetadata, StorageUnavailableError
from src.db.workspace_boundary_repo import (
    WorkspaceBoundaryRepositoryBase,
    get_workspace_boundary_repo,
)
from services.benchmark.artifact_store import S3BenchmarkArtifactStore

logger = logging.getLogger(__name__)


class BenchmarkPersistenceService:
    """Coordinates recoverable benchmark persistence across S3 and DynamoDB."""

    def __init__(
        self,
        boundary_repo: WorkspaceBoundaryRepositoryBase | None = None,
        artifact_store: S3BenchmarkArtifactStore | None = None,
    ) -> None:
        self.boundary_repo = boundary_repo or get_workspace_boundary_repo()
        self.artifact_store = artifact_store or S3BenchmarkArtifactStore()

    def persist_benchmark_run(
        self,
        workspace_id: str,
        metadata: BenchmarkRunMetadata,
        detailed_artifact: dict[str, Any],
    ) -> BenchmarkRunMetadata:
        """Persist benchmark execution results with S3 first recoverable ordering."""
        # Step 1: Write detailed JSON artifact to S3 first
        try:
            s3_key = self.artifact_store.save_artifact(
                workspace_id=workspace_id,
                benchmark_run_id=metadata.benchmark_run_id,
                artifact_payload=detailed_artifact,
            )
        except Exception as exc:
            logger.error(
                "Benchmark S3 artifact write failed for run %s: %s",
                metadata.benchmark_run_id,
                exc,
            )
            raise StorageUnavailableError(
                f"Failed to persist benchmark artifact to S3: {exc}"
            ) from exc

        # Step 2: Bind confirmed S3 key to compact metadata
        persisted_metadata = metadata.model_copy(update={"s3_artifact_key": s3_key})

        # Step 3: Persist compact metadata record to DynamoDB / boundary repo
        try:
            self.boundary_repo.save_benchmark_metadata(persisted_metadata)
            logger.info(
                "Successfully persisted benchmark run %s to DynamoDB with S3 key %s",
                metadata.benchmark_run_id,
                s3_key,
            )
            return persisted_metadata
        except Exception as exc:
            logger.error(
                "Benchmark DynamoDB metadata write failed for run %s (S3 artifact remains at %s): %s",
                metadata.benchmark_run_id,
                s3_key,
                exc,
            )
            raise StorageUnavailableError(
                f"Failed to persist benchmark metadata to DynamoDB: {exc}"
            ) from exc

    def get_benchmark_run(
        self,
        workspace_id: str,
        benchmark_run_id: str,
    ) -> tuple[BenchmarkRunMetadata | None, dict[str, Any] | None]:
        """Retrieve both compact metadata from DynamoDB and full artifact from S3."""
        metadata = self.boundary_repo.get_benchmark_metadata(workspace_id, benchmark_run_id)
        if not metadata:
            return None, None

        artifact = None
        if metadata.s3_artifact_key:
            artifact = self.artifact_store.get_artifact(metadata.s3_artifact_key)

        return metadata, artifact

    def list_benchmark_runs(self, workspace_id: str) -> list[BenchmarkRunMetadata]:
        """List all compact benchmark records scoped to a workspace."""
        return self.boundary_repo.list_benchmark_metadata(workspace_id)
