"""S3 storage adapter for detailed versioned benchmark artifacts.

Persists full query execution traces, latency distributions, citation details,
and evaluation comparisons to S3 before metadata is committed to DynamoDB.
"""

import json
import logging
from typing import Any

from config import settings

logger = logging.getLogger(__name__)


class S3BenchmarkArtifactStore:
    """Stores detailed benchmark JSON artifacts in Amazon S3 or LocalStack."""

    def __init__(self, s3_client: Any | None = None, bucket_name: str | None = None) -> None:
        self._s3 = s3_client
        self._bucket = bucket_name or getattr(settings, "S3_BENCHMARK_BUCKET", "kre-benchmarks")

    def _get_client(self) -> Any:
        if self._s3 is not None:
            return self._s3
        from aws.infra import get_client
        return get_client("s3")

    def save_artifact(
        self,
        workspace_id: str,
        benchmark_run_id: str,
        artifact_payload: dict[str, Any],
    ) -> str:
        """Upload detailed benchmark JSON report to S3.

        Returns the exact S3 key where the artifact is persisted.
        """
        s3_key = f"benchmarks/{workspace_id}/{benchmark_run_id}.json"
        raw_json = json.dumps(artifact_payload, indent=2, default=str).encode("utf-8")

        client = self._get_client()
        try:
            client.put_object(
                Bucket=self._bucket,
                Key=s3_key,
                Body=raw_json,
                ContentType="application/json",
            )
            logger.info("Saved benchmark artifact to s3://%s/%s", self._bucket, s3_key)
            return s3_key
        except Exception as exc:
            logger.error("Failed to upload benchmark artifact to s3://%s/%s: %s", self._bucket, s3_key, exc)
            raise

    def get_artifact(self, s3_key: str) -> dict[str, Any] | None:
        """Retrieve and parse benchmark JSON artifact from S3."""
        client = self._get_client()
        try:
            resp = client.get_object(Bucket=self._bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8")
            return json.loads(body)
        except Exception as exc:
            logger.warning("Failed to retrieve benchmark artifact from s3://%s/%s: %s", self._bucket, s3_key, exc)
            return None
