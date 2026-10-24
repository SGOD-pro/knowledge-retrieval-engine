"""Durable persistence for PageIndex structural index.

Enforces S3-first recoverable write ordering for payloads exceeding 250KB,
summary bounding without silent truncation, workspace/document/version scoping,
and immediate tombstone enforcement.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from config import settings
from src.db.workspace_boundary_repo import (
    WorkspaceBoundaryRepositoryBase,
    get_workspace_boundary_repo,
)
from src.ingestion.page_index_extractor import LayoutExtractionResult
from src.schemas.contracts.boundary import StorageUnavailableError
from src.schemas.contracts.page_index import (
    CrossReference,
    ElementLocation,
    PageIndexDetail,
    PageIndexSummary,
    SectionSummary,
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


class S3PageIndexArtifactStore:
    """Stores full PageIndexDetail JSON topology in Amazon S3 or LocalStack."""

    def __init__(
        self,
        s3_client: Any | None = None,
        bucket_name: str | None = None,
        connect_timeout: float = 0.030,
        read_timeout: float = 0.040,
    ) -> None:
        self._s3 = s3_client
        self._bucket = bucket_name or getattr(settings, "S3_PAGEINDEX_BUCKET", "kre-storage")
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._in_memory_store: dict[str, str] = {}

    def _get_client(self) -> Any:
        if self._s3 is not None:
            return self._s3

        # In dev or test when s3 is not configured, fallback to in-memory store
        env = getattr(settings, "ENVIRONMENT", "test").lower()
        if env in ("test", "dev"):
            return None

        import boto3
        from aws.infra import get_client

        cfg = create_dedicated_storage_config(self._connect_timeout, self._read_timeout)
        try:
            client = get_client("s3", config=cfg)
        except Exception:
            session = boto3.Session()
            client = session.client(
                "s3",
                region_name=getattr(settings, "AWS_REGION", "ap-south-1"),
                config=cfg,
            )
        disable_client_retries(client)
        return client

    def save_detail(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        detail: PageIndexDetail,
    ) -> str:
        """Upload full PageIndexDetail topology to S3 with recoverable key."""
        s3_key = f"pageindex/{workspace_id}/{document_id}/{document_version}/hierarchy.json"
        raw_json = detail.model_dump_json(indent=2).encode("utf-8")

        client = self._get_client()
        if client is None:
            self._in_memory_store[s3_key] = raw_json.decode("utf-8")
            logger.info("Saved PageIndex detail to in-memory fallback: %s", s3_key)
            return s3_key

        try:
            client.put_object(
                Bucket=self._bucket,
                Key=s3_key,
                Body=raw_json,
                ContentType="application/json",
            )
            logger.info("Saved PageIndex detail to s3://%s/%s", self._bucket, s3_key)
            return s3_key
        except Exception as exc:
            logger.error("Failed to upload PageIndex detail to s3://%s/%s: %s", self._bucket, s3_key, exc)
            raise StorageUnavailableError(f"Failed to persist PageIndex detail to S3: {exc}") from exc

    def get_detail(self, s3_key: str, remaining_deadline_s: float | None = None) -> PageIndexDetail | None:
        """Retrieve and parse PageIndexDetail from S3 within remaining deadline."""
        if remaining_deadline_s is not None and remaining_deadline_s <= 0:
            logger.warning("PageIndex S3 read skipped: deadline expired")
            return None

        client = self._get_client()
        if client is None:
            raw_text = self._in_memory_store.get(s3_key)
            if not raw_text:
                return None
            try:
                data = json.loads(raw_text)
                return self._parse_detail(data)
            except Exception as exc:
                logger.warning("Failed to parse in-memory PageIndex detail: %s", exc)
                return None

        try:
            resp = client.get_object(Bucket=self._bucket, Key=s3_key)
            body = resp["Body"].read().decode("utf-8")
            data = json.loads(body)
            return self._parse_detail(data)
        except Exception as exc:
            logger.warning("Failed to retrieve PageIndex detail from s3://%s/%s: %s", self._bucket, s3_key, exc)
            return None

    def delete_detail(self, s3_key: str) -> None:
        """Delete detail artifact from S3 or in-memory fallback."""
        self._in_memory_store.pop(s3_key, None)
        client = self._get_client()
        if client is not None:
            try:
                client.delete_object(Bucket=self._bucket, Key=s3_key)
            except Exception as exc:
                logger.debug("Failed to delete S3 artifact %s: %s", s3_key, exc)

    def _parse_detail(self, data: dict[str, Any]) -> PageIndexDetail:
        sections = tuple(SectionSummary(**s) for s in data.get("sections", []))
        definitions = {
            k: tuple(ElementLocation(**loc) for loc in locs)
            for k, locs in data.get("definitions", {}).items()
        }
        cross_refs = {
            k: tuple(CrossReference(**cr) for cr in crs)
            for k, crs in data.get("cross_references", {}).items()
        }
        return PageIndexDetail(
            workspace_id=data["workspace_id"],
            document_id=data["document_id"],
            document_version=data["document_version"],
            sections=sections,
            definitions=definitions,
            cross_references=cross_refs,
        )


class PageIndexRepository:
    """Authoritative repository for PageIndex summaries and detail overflow."""

    def __init__(
        self,
        boundary_repo: WorkspaceBoundaryRepositoryBase | None = None,
        artifact_store: S3PageIndexArtifactStore | None = None,
    ) -> None:
        self.boundary_repo = boundary_repo or get_workspace_boundary_repo()
        self.artifact_store = artifact_store or S3PageIndexArtifactStore()

    def persist_manifest(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        layout: LayoutExtractionResult,
        threshold_bytes: int | None = None,
        created_at: str | None = None,
    ) -> PageIndexSummary:
        """Persist PageIndex manifest enforcing S3-first write ordering on overflow.

        Bounding Rule:
        - Entire summary is bounded within threshold_bytes (default 250KB).
        - If manifest exceeds threshold, full detail topology is written to S3 first.
        - If root_sections itself is large, root_sections is bounded to top-level sections
          and marked with sections_overflowed=True without silent truncation (100% of
          sections are preserved in S3).
        """
        threshold = threshold_bytes or getattr(settings, "PAGEINDEX_OVERFLOW_THRESHOLD_BYTES", 250000)
        timestamp = created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Build full detail object holding 100% of the extracted topology
        detail = PageIndexDetail(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            sections=layout.sections,
            definitions=layout.definitions,
            cross_references=layout.cross_references,
        )

        # Estimate serialized size of full inline summary
        candidate_summary_dict = {
            "workspace_id": workspace_id,
            "document_id": document_id,
            "document_version": document_version,
            "total_pages": layout.total_pages,
            "layout_status": layout.layout_status,
            "root_sections": [s.model_dump() for s in layout.sections],
            "has_s3_overflow": False,
            "s3_overflow_key": None,
            "inline_definitions": {
                k: [loc.model_dump() for loc in locs]
                for k, locs in layout.definitions.items()
            },
            "created_at": timestamp,
            "structural_elements_count": layout.structural_elements_count,
            "raw_payload_bytes": 0,
            "sections_overflowed": False,
        }
        raw_bytes = len(json.dumps(candidate_summary_dict).encode("utf-8"))
        candidate_summary_dict["raw_payload_bytes"] = raw_bytes

        inline_crs = {
            k: tuple(sorted(list({cr.page_number for cr in crs})))
            for k, crs in layout.cross_references.items()
        }

        if raw_bytes <= threshold:
            # Fits inline within DynamoDB 250KB bound
            summary = PageIndexSummary(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                total_pages=layout.total_pages,
                layout_status=layout.layout_status,
                root_sections=layout.sections,
                has_s3_overflow=False,
                s3_overflow_key=None,
                inline_definitions=layout.definitions,
                inline_cross_references=inline_crs,
                created_at=timestamp,
                structural_elements_count=layout.structural_elements_count,
                raw_payload_bytes=raw_bytes,
                sections_overflowed=False,
            )
            self.boundary_repo.save_page_index_summary(summary)
            logger.info(
                "page_index.persisted_inline ws=%s doc=%s ver=%s bytes=%d",
                workspace_id,
                document_id,
                document_version,
                raw_bytes,
            )
            return summary

        # Step 1: Payload exceeds threshold. Write complete detail to S3 FIRST (S3-first ordering)
        try:
            s3_key = self.artifact_store.save_detail(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                detail=detail,
            )
        except Exception as exc:
            logger.error("PageIndex S3 write failed for doc %s ver %s: %s", document_id, document_version, exc)
            raise StorageUnavailableError(f"Failed to persist PageIndex detail to S3: {exc}") from exc

        # Step 2: Bound root_sections if sections alone would threaten item size limit
        sections_raw = json.dumps([s.model_dump() for s in layout.sections]).encode("utf-8")
        sections_overflowed = False
        bounded_sections: tuple[SectionSummary, ...] = layout.sections

        # If sections payload exceeds 40% of threshold, retain only top-level sections in summary (capped to 50)
        if len(sections_raw) > (threshold * 0.4):
            sections_overflowed = True
            top_level = [s for s in layout.sections if s.level == 1]
            if not top_level:
                top_level = list(layout.sections[:50])
            else:
                top_level = top_level[:50]
            bounded_sections = tuple(top_level)
            logger.info(
                "page_index.sections_bounded_to_s3 ws=%s doc=%s total_sections=%d summary_sections=%d",
                workspace_id,
                document_id,
                len(layout.sections),
                len(bounded_sections),
            )

        # Step 3: Persist bounded PageIndexSummary record to DynamoDB / SQLite
        summary = PageIndexSummary(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            total_pages=layout.total_pages,
            layout_status=layout.layout_status,
            root_sections=bounded_sections,
            has_s3_overflow=True,
            s3_overflow_key=s3_key,
            inline_definitions={},  # definitions offloaded to S3 detail
            created_at=timestamp,
            structural_elements_count=layout.structural_elements_count,
            raw_payload_bytes=raw_bytes,
            sections_overflowed=sections_overflowed,
        )

        try:
            self.boundary_repo.save_page_index_summary(summary)
            logger.info(
                "page_index.persisted_overflow ws=%s doc=%s ver=%s s3_key=%s",
                workspace_id,
                document_id,
                document_version,
                s3_key,
            )
            return summary
        except Exception as exc:
            logger.error("PageIndex DynamoDB summary write failed for doc %s (cleaning up S3 artifact at %s): %s", document_id, s3_key, exc)
            if s3_key:
                try:
                    self.artifact_store.delete_detail(s3_key)
                except Exception:
                    pass
            raise StorageUnavailableError(f"Failed to persist PageIndex summary: {exc}") from exc

    def get_summary(
        self, workspace_id: str, document_id: str, document_version: str
    ) -> PageIndexSummary | None:
        """Retrieve PageIndexSummary checking active tombstones first."""
        # Active workspace or source tombstone immediately blocks retrieval with 0 leakage
        if self.boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
            logger.debug("page_index.tombstone_blocked workspace_id=%s", workspace_id)
            return None
        if self.boundary_repo.has_tombstone(workspace_id, "source", document_id):
            logger.debug("page_index.tombstone_blocked source_id=%s", document_id)
            return None

        return self.boundary_repo.get_page_index_summary(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
        )

    def get_detail(
        self, summary: PageIndexSummary, remaining_deadline_s: float | None = None
    ) -> PageIndexDetail | None:
        """Retrieve full PageIndexDetail either from S3 or reconstructed from inline summary."""
        if summary.has_s3_overflow and summary.s3_overflow_key:
            return self.artifact_store.get_detail(
                summary.s3_overflow_key, remaining_deadline_s=remaining_deadline_s
            )

        # Reconstruct detail from inline summary
        return PageIndexDetail(
            workspace_id=summary.workspace_id,
            document_id=summary.document_id,
            document_version=summary.document_version,
            sections=summary.root_sections,
            definitions=summary.inline_definitions,
            cross_references={},
        )
