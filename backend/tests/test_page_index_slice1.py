"""Focused offline regression test suite for Proposed 0005 Slice 1:
PDF structural extraction → durable index → isolated lookup.

Covers:
- Definition targets versus body/table-cell cross-references.
- Duplicate labels and missing-layout states.
- Workspace/document/version scope and tombstones.
- Inline and S3 overflow persistence with S3-first ordering.
- Summary bounding including root_sections without silent truncation.
- Admission queue bounding and load shedding.
- Capacity retention until underlying storage work actually finishes.
- Distinction between caller timeout and storage cancellation.
- Deadline enforcement across storage reads and explicit SDK retry disabling.
- Baseline ingestion preservation when enrichment fails.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch
import pytest

from src.db.page_index_repo import (
    PageIndexRepository,
    S3PageIndexArtifactStore,
    create_dedicated_storage_config,
    disable_client_retries,
)
from src.db.workspace_boundary_repo import (
    InMemoryWorkspaceBoundaryRepository,
    SQLiteWorkspaceBoundaryRepository,
)
from src.ingestion.page_index_extractor import (
    extract_layout_from_elements,
    normalize_label,
)
from src.ingestion.page_index_service import (
    extract_and_persist_page_index,
)
from src.schemas.contracts.boundary import (
    DocumentSnapshot,
    StorageUnavailableError,
)
from src.schemas.contracts.page_index import (
    ElementLocation,
    PageIndexDetail,
    PageIndexSummary,
    SectionSummary,
)
from src.services.retrieval.page_index_lookup import (
    PageIndexConcurrencyController,
    PageIndexLookupService,
)
from src.services.retrieval.page_index_retriever import PageIndexRetriever


# ===========================================================================
# 1. Target Discrimination: Definitions vs Body/Cell Cross-References
# ===========================================================================

def test_definition_targets_vs_body_and_cell_cross_references():
    """Verify formal definitions are indexed while body and cell mentions become cross-references."""
    elements = [
        # Page 42: Formal definition caption and table
        {
            "id": "doc1:p42:cap",
            "type": "caption",
            "content": "Table 8: Annual Operating Financials",
            "page_number": 42,
        },
        {
            "id": "doc1:p42:tbl",
            "type": "table",
            "content": "Table 8 data rows and figures",
            "page_number": 42,
        },
        # Page 10: Running paragraph mentioning Table 8
        {
            "id": "doc1:p10:p1",
            "type": "paragraph",
            "content": "As shown in Table 8 on page 42, gross revenue increased by 14 percent.",
            "page_number": 10,
        },
        # Page 22: Table cell mentioning Table 8
        {
            "id": "doc1:p22:c1",
            "type": "cell",
            "content": "See Table 8 for full breakdown",
            "page_number": 22,
        },
        # Page 15: Figure definition caption
        {
            "id": "doc1:p15:fig",
            "type": "caption",
            "content": "Figure 2.1: Architectural Data Flow",
            "page_number": 15,
        },
        # Page 18: Running paragraph referencing Figure 2.1
        {
            "id": "doc1:p18:p1",
            "type": "paragraph",
            "content": "Refer to Figure 2.1 for a high level architecture overview.",
            "page_number": 18,
        },
    ]

    layout = extract_layout_from_elements(elements, total_pages=50, document_id="doc1")

    # Assert definitions: only page 42 for table 8, only page 15 for figure 2.1
    assert "table 8" in layout.definitions
    table_8_defs = layout.definitions["table 8"]
    assert len(table_8_defs) == 2  # caption and table element on page 42
    assert all(loc.page_number == 42 for loc in table_8_defs)

    assert "figure 2.1" in layout.definitions
    fig_defs = layout.definitions["figure 2.1"]
    assert len(fig_defs) == 1
    assert fig_defs[0].page_number == 15

    # Assert cross-references: pages 10 and 22 for table 8; page 18 for figure 2.1
    assert "table 8" in layout.cross_references
    t8_cr_pages = {cr.page_number for cr in layout.cross_references["table 8"]}
    assert t8_cr_pages == {10, 22}

    assert "figure 2.1" in layout.cross_references
    fig_cr_pages = {cr.page_number for cr in layout.cross_references["figure 2.1"]}
    assert fig_cr_pages == {18}


def test_cross_reference_only_query_resolution():
    """Verify querying a target that only exists in cross-references returns state='none' with cross-ref info."""
    elements = [
        {
            "id": "doc1:h1",
            "type": "heading",
            "content": "Section 1: Filing Overview",
            "page_number": 1,
        },
        {
            "id": "doc1:p12:p",
            "type": "paragraph",
            "content": "For external context, refer to Table 9 in the previous annual filing.",
            "page_number": 12,
        },
    ]
    layout = extract_layout_from_elements(elements, total_pages=20, document_id="doc1")
    assert layout.layout_status == "ready"
    assert "table 9" not in layout.definitions
    assert "table 9" in layout.cross_references

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)
    summary = pi_repo.persist_manifest("ws_test", "doc1", "v1", layout)

    lookup_service = PageIndexLookupService(repository=pi_repo)
    res = lookup_service.resolve_target_from_manifest(summary, "Table 9")
    assert res.state == "none"
    assert res.candidate_pages == ()
    assert res.has_cross_references_only is True
    assert res.cross_reference_pages == (12,)


# ===========================================================================
# 2. Duplicate Labels and Missing Layout States
# ===========================================================================

def test_duplicate_labels_handled_as_ambiguous():
    """Verify duplicate definitions across chapters or appendices resolve to state='ambiguous' with all pages."""
    elements = [
        # Chapter 1 Table 1 on page 5
        {
            "id": "doc1:p5:t1",
            "type": "table",
            "content": "Table 1: Main Study Cohort",
            "page_number": 5,
        },
        # Appendix Table 1 on page 40
        {
            "id": "doc1:p40:t1",
            "type": "table",
            "content": "Table 1: Appendix Survey Demographics",
            "page_number": 40,
        },
    ]

    layout = extract_layout_from_elements(elements, total_pages=50, document_id="doc1")
    assert "table 1" in layout.definitions
    assert len(layout.definitions["table 1"]) == 2

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)
    summary = pi_repo.persist_manifest("ws_test", "doc1", "v1", layout)

    lookup_service = PageIndexLookupService(repository=pi_repo)
    res = lookup_service.resolve_target_from_manifest(summary, "Table 1")

    assert res.state == "ambiguous"
    assert res.candidate_pages == (5, 40)
    assert len(res.matching_locations) == 2


def test_missing_layout_state():
    """Verify flat unstructured text PDF records layout_status='missing_layout' and returns empty candidates."""
    elements = [
        {"id": "doc1:p1", "type": "paragraph", "content": "Just running body text without headers.", "page_number": 1},
        {"id": "doc1:p2", "type": "paragraph", "content": "More ordinary body text on second page.", "page_number": 2},
    ]

    layout = extract_layout_from_elements(elements, total_pages=2, document_id="doc1")
    assert layout.layout_status == "missing_layout"
    assert len(layout.sections) == 0
    assert len(layout.definitions) == 0

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)
    summary = pi_repo.persist_manifest("ws_test", "doc1", "v1", layout)
    assert summary.layout_status == "missing_layout"

    lookup_service = PageIndexLookupService(repository=pi_repo)
    res = lookup_service.resolve_target_from_manifest(summary, "Table 1")
    assert res.state == "missing_layout"
    assert res.candidate_pages == ()


# ===========================================================================
# 3. Workspace/Document/Version Scope and Tombstones
# ===========================================================================

def test_workspace_and_version_partitioning():
    """Verify manifests are partitioned strictly by workspace_id, document_id, and version."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)

    elements = [
        {"id": "d1:t1", "type": "table", "content": "Table 1: Data", "page_number": 3}
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_alpha")

    # Persist in ws_alpha under v1
    pi_repo.persist_manifest("ws_alpha", "doc_alpha", "v1", layout)

    # 1. Correct query retrieves summary
    s1 = pi_repo.get_summary("ws_alpha", "doc_alpha", "v1")
    assert s1 is not None
    assert s1.workspace_id == "ws_alpha"

    # 2. Querying wrong workspace returns None (0 cross-tenant leakage)
    s2 = pi_repo.get_summary("ws_beta", "doc_alpha", "v1")
    assert s2 is None

    # 3. Querying wrong version returns None
    s3 = pi_repo.get_summary("ws_alpha", "doc_alpha", "v2")
    assert s3 is None


@pytest.mark.anyio
async def test_tombstone_blocks_lookup_immediately():
    """Verify active source or workspace tombstone immediately returns 'tombstoned' with zero leakage."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)

    elements = [
        {"id": "d1:t1", "type": "table", "content": "Table 1: Secret Data", "page_number": 3}
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_tomb")
    pi_repo.persist_manifest("ws_tomb", "doc_tomb", "v1", layout)

    retriever = PageIndexRetriever(lookup_service=PageIndexLookupService(repository=pi_repo))

    # Before tombstone: succeeds
    res_before, status_before = await retriever.lookup("ws_tomb", "doc_tomb", "v1", "Table 1")
    assert status_before == "completed"
    assert res_before is not None
    assert res_before.candidate_pages == (3,)

    # Add source tombstone
    boundary_repo.record_source_tombstone("ws_tomb", "doc_tomb", "admin_caller", "deleted")

    # After source tombstone: immediately returns tombstoned with empty candidate result
    res_after, status_after = await retriever.lookup("ws_tomb", "doc_tomb", "v1", "Table 1")
    assert status_after == "tombstoned"
    assert res_after is None

    # Workspace tombstone also blocks
    boundary_repo.tombstone_workspace("ws_tomb", "admin_caller", "deleted")
    res_ws, status_ws = await retriever.lookup("ws_tomb", "doc_tomb", "v1", "Table 1")
    assert status_ws == "tombstoned"
    assert res_ws is None


# ===========================================================================
# 4. Persistence: Inline vs S3 Overflow with S3-First Ordering
# ===========================================================================

def test_inline_persistence_under_threshold():
    """Verify payloads under 250KB store inline definitions in DynamoDB/repository."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.create_workspace("Test Tomb", "admin_caller", workspace_id="ws_tomb")
    boundary_repo.create_source("ws_tomb", "Secret Data", source_id="doc_tomb")
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)

    elements = [
        {"id": f"elem_{i}", "type": "table", "content": f"Table {i}: Caption {i}", "page_number": i}
        for i in range(1, 10)
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_small")
    summary = pi_repo.persist_manifest("ws_test", "doc_small", "v1", layout, threshold_bytes=250000)

    assert summary.has_s3_overflow is False
    assert summary.s3_overflow_key is None
    assert len(summary.inline_definitions) == 9
    assert summary.sections_overflowed is False


def test_s3_overflow_with_s3_first_ordering():
    """Verify payloads over 250KB store detail to S3 first, then write bounded summary."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    artifact_store = S3PageIndexArtifactStore()
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo, artifact_store=artifact_store)

    # Generate synthetic payload exceeding 5000 bytes threshold
    elements = [
        {"id": f"elem_{i}", "type": "table", "content": f"Table {i}: " + ("x" * 200), "page_number": i}
        for i in range(1, 40)
    ]
    layout = extract_layout_from_elements(elements, total_pages=40, document_id="doc_large")

    # Persist with small 2KB threshold to trigger overflow
    summary = pi_repo.persist_manifest("ws_test", "doc_large", "v1", layout, threshold_bytes=2000)

    assert summary.has_s3_overflow is True
    assert summary.s3_overflow_key is not None
    assert "hierarchy.json" in summary.s3_overflow_key
    assert len(summary.inline_definitions) == 0  # Offloaded to S3

    # Verify detail is retrievable from S3
    detail = pi_repo.get_detail(summary)
    assert detail is not None
    assert len(detail.definitions) == 39


def test_s3_write_failure_aborts_dynamodb_persistence():
    """Verify S3-first ordering: if S3 upload fails, DynamoDB write is aborted with no orphaned record."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    mock_artifact_store = MagicMock(spec=S3PageIndexArtifactStore)
    mock_artifact_store.save_detail.side_effect = RuntimeError("S3 connection dropped")

    pi_repo = PageIndexRepository(boundary_repo=boundary_repo, artifact_store=mock_artifact_store)

    elements = [
        {"id": f"elem_{i}", "type": "table", "content": f"Table {i}: " + ("x" * 200), "page_number": i}
        for i in range(1, 40)
    ]
    layout = extract_layout_from_elements(elements, total_pages=40, document_id="doc_fail")

    with pytest.raises(StorageUnavailableError):
        pi_repo.persist_manifest("ws_test", "doc_fail", "v1", layout, threshold_bytes=2000)

    # Verify no record was committed to boundary repo
    saved_summary = boundary_repo.get_page_index_summary("ws_test", "doc_fail", "v1")
    assert saved_summary is None


# ===========================================================================
# 5. Bounding Serialized Summary Including root_sections
# ===========================================================================

def test_bounding_root_sections_without_silent_truncation():
    """Verify that when root_sections is very large, it is bounded in summary without dropping S3 detail."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    artifact_store = S3PageIndexArtifactStore()
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo, artifact_store=artifact_store)

    # Outline with 100 sections
    outline = [
        (f"Section {i} Title " + ("y" * 50), 1 if i % 10 == 0 else 2, i, i + 1)
        for i in range(1, 101)
    ]
    elements = [
        {"id": "elem_1", "type": "paragraph", "content": "Sample text", "page_number": 1}
    ]
    layout = extract_layout_from_elements(
        elements, total_pages=100, outline_items=outline, document_id="doc_sections"
    )
    assert len(layout.sections) == 100

    # Persist with threshold of 3KB: sections payload will exceed 40% of threshold
    summary = pi_repo.persist_manifest(
        "ws_test", "doc_sections", "v1", layout, threshold_bytes=3000
    )

    assert summary.has_s3_overflow is True
    assert summary.sections_overflowed is True
    # Bounded summary retains only top-level sections (level 1)
    assert len(summary.root_sections) < 100
    assert all(s.level == 1 for s in summary.root_sections)

    # S3 detail retains 100% of all 100 sections without silent truncation!
    detail = pi_repo.get_detail(summary)
    assert detail is not None
    assert len(detail.sections) == 100


# ===========================================================================
# 6. Concurrency: Bounded Admission and Load Shedding
# ===========================================================================

@pytest.mark.anyio
async def test_admission_and_queued_submissions_bounded():
    """Verify concurrency controller rejects submissions immediately when queue is full."""
    # Controller with capacity 1 and queue 1 (max 2 in flight/waiting total)
    controller = PageIndexConcurrencyController(max_concurrency=1, max_queue=1)

    start_event = asyncio.Event()

    def slow_worker(*args):
        # Synchronous sleep inside thread
        time.sleep(0.08)
        return "ok"

    # Task 1 occupies worker slot
    t1 = asyncio.create_task(controller.run_storage_work(slow_worker, deadline_s=0.2))
    await asyncio.sleep(0.01)

    # Task 2 enters queue
    t2 = asyncio.create_task(controller.run_storage_work(slow_worker, deadline_s=0.2))
    await asyncio.sleep(0.01)

    # Task 3 arrives: queue is full (queued >= max_queue) -> rejected immediately!
    res3, status3 = await controller.run_storage_work(slow_worker, deadline_s=0.2)
    assert status3 == "admission_rejected"
    assert res3 is None

    # Wait for t1 and t2 to complete
    r1, s1 = await t1
    r2, s2 = await t2
    assert s1 == "completed"
    assert s2 == "completed"


# ===========================================================================
# 7. Capacity Retention Until Underlying Storage Finishes
# ===========================================================================

@pytest.mark.anyio
async def test_retains_capacity_until_underlying_thread_finishes():
    """Verify that when a caller times out, worker capacity remains retained until the thread exits."""
    controller = PageIndexConcurrencyController(max_concurrency=1, max_queue=2)

    thread_finished = False

    def lingering_worker(*args):
        nonlocal thread_finished
        time.sleep(0.06)
        thread_finished = True
        return "done"

    # Caller deadline is 20ms (worker takes 60ms)
    t0 = time.perf_counter()
    res, status = await controller.run_storage_work(lingering_worker, deadline_s=0.020)

    # Caller timed out
    assert status == "caller_timeout"
    assert res is None
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.040

    # At this point, the caller has received timeout, but the thread is still running!
    # Therefore, active_workers must still be 1 (retained)!
    assert controller.active_count == 1
    assert thread_finished is False

    # Wait for the thread to actually finish
    await asyncio.sleep(0.06)
    assert thread_finished is True
    # Now capacity is released
    assert controller.active_count == 0


# ===========================================================================
# 8. Distinguish Caller Timeout from Storage Cancellation
# ===========================================================================

@pytest.mark.anyio
async def test_distinguish_caller_timeout_from_cancellation():
    """Verify caller timeout is distinguished from caller task cancellation."""
    controller = PageIndexConcurrencyController(max_concurrency=2, max_queue=2)

    def slow_task(*args):
        time.sleep(0.08)
        return "ok"

    # 1. Caller timeout test
    res_to, status_to = await controller.run_storage_work(slow_task, deadline_s=0.02)
    assert status_to == "caller_timeout"

    # 2. Coroutine cancellation test
    task = asyncio.create_task(controller.run_storage_work(slow_task, deadline_s=0.5))
    await asyncio.sleep(0.01)
    task.cancel()

    try:
        res_c, status_c = await task
        assert status_c == "caller_cancelled"
    except asyncio.CancelledError:
        pass


# ===========================================================================
# 9. Deadlines Across Storage Reads and Explicit SDK Retry Disabling
# ===========================================================================

def test_dedicated_storage_config_and_retry_disabling():
    """Verify dedicated socket configuration and explicit retry handler removal on real botocore clients."""
    cfg = create_dedicated_storage_config(connect_timeout=0.030, read_timeout=0.040)
    assert cfg.connect_timeout == 0.030
    assert cfg.read_timeout == 0.040
    assert cfg.retries == {"max_attempts": 1, "mode": "standard"}

    import boto3
    s3_client = boto3.client("s3", region_name="us-east-1", config=cfg)
    ddb_client = boto3.client("dynamodb", region_name="us-east-1", config=cfg)

    # Before disabling, retry handlers exist
    s3_retries_before = [k for k in getattr(s3_client.meta.events._emitter, "_unique_id_handlers", {}).keys() if "retry" in k]
    assert len(s3_retries_before) > 0

    disable_client_retries(s3_client)
    disable_client_retries(ddb_client)

    # After disabling, retry handlers are completely unregistered
    s3_retries_after = [k for k in getattr(s3_client.meta.events._emitter, "_unique_id_handlers", {}).keys() if "retry" in k]
    ddb_retries_after = [k for k in getattr(ddb_client.meta.events._emitter, "_unique_id_handlers", {}).keys() if "retry" in k]
    assert s3_retries_after == []
    assert ddb_retries_after == []


@pytest.mark.anyio
async def test_deadlines_enforced_across_dynamodb_and_s3():
    """Verify 50ms deadline caps total storage lookup across DynamoDB and S3."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    artifact_store = S3PageIndexArtifactStore()
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo, artifact_store=artifact_store)

    elements = [
        {"id": f"elem_{i}", "type": "table", "content": f"Table {i}: " + ("z" * 300), "page_number": i}
        for i in range(1, 20)
    ]
    layout = extract_layout_from_elements(elements, total_pages=20, document_id="doc_ddl")
    pi_repo.persist_manifest("ws_test", "doc_ddl", "v1", layout, threshold_bytes=1000)

    # Inject slow delay into artifact_store.get_detail
    original_get = artifact_store.get_detail

    def slow_s3_get(s3_key, remaining_deadline_s=None):
        time.sleep(0.08)
        return original_get(s3_key, remaining_deadline_s)

    artifact_store.get_detail = slow_s3_get

    lookup_service = PageIndexLookupService(repository=pi_repo)
    t0 = time.perf_counter()
    res, status = await lookup_service.lookup_target("ws_test", "doc_ddl", "v1", "Table 1", deadline_ms=30)
    elapsed = time.perf_counter() - t0

    assert status == "caller_timeout"
    assert res is None
    assert elapsed < 0.050


# ===========================================================================
# 10. Baseline Ingestion Preservation on Enrichment Failure
# ===========================================================================

def test_preserve_baseline_ingestion_when_enrichment_fails():
    """Verify that if PageIndex extraction fails, baseline ingestion and DocumentSnapshot remain intact."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()

    # Pre-seed baseline snapshot
    baseline_snap = DocumentSnapshot(
        workspace_id="ws_baseline",
        document_id="doc_fail_test",
        source="test.pdf",
        version="v1",
        format="pdf",
        persisted_rows=10,
        total_rows=10,
        created_at="2026-10-24T00:00:00Z",
    )
    boundary_repo.save_document_snapshot(baseline_snap)

    mock_bad_repo = MagicMock(spec=PageIndexRepository)
    mock_bad_repo.persist_manifest.side_effect = RuntimeError("Storage crash during index write")

    # Ingestion enrichment call does not raise
    result = extract_and_persist_page_index(
        path=None,
        document_id="doc_fail_test",
        workspace_id="ws_baseline",
        document_version="v1",
        chunks=(),
        boundary_repo=boundary_repo,
        page_index_repo=mock_bad_repo,
    )
    assert result is None  # Handled cleanly

    # Baseline snapshot is preserved with page_index_status="failed"
    updated_snap = boundary_repo.get_document_snapshot("ws_baseline", "doc_fail_test")
    assert updated_snap is not None
    assert updated_snap.persisted_rows == 10
    assert updated_snap.page_index_status == "failed"


# ===========================================================================
# 11. SQLite Repository Persistence & Migration
# ===========================================================================

def test_sqlite_repository_persistence_and_schema():
    """Verify SQLite repository stores and retrieves PageIndexSummary and DocumentSnapshot attributes."""
    sqlite_repo = SQLiteWorkspaceBoundaryRepository(db_path=":memory:")

    # DocumentSnapshot with PageIndex fields
    snap = DocumentSnapshot(
        workspace_id="ws_sqlite",
        document_id="doc_sql_1",
        source="report.pdf",
        version="v1",
        format="pdf",
        persisted_rows=5,
        total_rows=5,
        created_at="2026-10-24T00:00:00Z",
        page_index_status="ready",
        page_index_elements_count=3,
    )
    sqlite_repo.save_document_snapshot(snap)

    retrieved_snap = sqlite_repo.get_document_snapshot("ws_sqlite", "doc_sql_1")
    assert retrieved_snap is not None
    assert retrieved_snap.page_index_status == "ready"
    assert retrieved_snap.page_index_elements_count == 3

    # PageIndexSummary
    loc = ElementLocation(
        element_id="elem_1",
        page_number=2,
        element_type="table",
        label="table 1",
        title="Revenue Table",
    )
    sec = SectionSummary(title="Introduction", level=1, page_start=1, page_end=3)
    summary = PageIndexSummary(
        workspace_id="ws_sqlite",
        document_id="doc_sql_1",
        document_version="v1",
        total_pages=3,
        layout_status="ready",
        root_sections=(sec,),
        has_s3_overflow=False,
        inline_definitions={"table 1": (loc,)},
        created_at="2026-10-24T00:00:00Z",
        structural_elements_count=2,
    )
    sqlite_repo.save_page_index_summary(summary)

    retrieved_sum = sqlite_repo.get_page_index_summary("ws_sqlite", "doc_sql_1", "v1")
    assert retrieved_sum is not None
    assert retrieved_sum.total_pages == 3
    assert len(retrieved_sum.root_sections) == 1
    assert retrieved_sum.root_sections[0].title == "Introduction"
    assert "table 1" in retrieved_sum.inline_definitions
    assert retrieved_sum.inline_definitions["table 1"][0].page_number == 2


def test_same_page_caption_and_table_is_unique_not_ambiguous():
    """Verify target with caption and table element on the same page is unique, not ambiguous."""
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo)
    lookup_service = PageIndexLookupService(repository=pi_repo)

    # Document where Table 1 has both caption and table element on page 7
    elements = [
        {"id": "c1", "type": "caption", "content": "Table 1: Financial Performance", "page_number": 7},
        {"id": "t1", "type": "table", "content": "Table 1: Financial Performance\nQ1 | $10M", "page_number": 7},
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_unique")
    sum_unique = pi_repo.persist_manifest("ws_test", "doc_unique", "v1", layout)
    res = lookup_service.resolve_target_from_manifest(sum_unique, "Table 1")
    assert res.state == "unique"
    assert res.candidate_pages == (7,)
    assert len(res.matching_locations) == 2

    # Now contrast with true ambiguous target spanning two distinct pages (page 7 and page 9)
    elements_ambiguous = [
        {"id": "c1", "type": "caption", "content": "Table 1: Financial Performance", "page_number": 7},
        {"id": "c2", "type": "caption", "content": "Table 1: Second Performance", "page_number": 9},
    ]
    layout_ambiguous = extract_layout_from_elements(elements_ambiguous, total_pages=10, document_id="doc_ambig")
    sum_ambig = pi_repo.persist_manifest("ws_test", "doc_ambig", "v1", layout_ambiguous)

    res_ambig = lookup_service.resolve_target_from_manifest(sum_ambig, "Table 1")
    assert res_ambig.state == "ambiguous"
    assert res_ambig.candidate_pages == (7, 9)
    assert len(res_ambig.matching_locations) == 2


def test_unanchored_reference_in_table_cell_does_not_define_target():
    """Verify unanchored mention of another table inside a cell is indexed as cross-reference, not definition."""
    elements = [
        # Table 2 on page 5 with cell referring to Table 4
        {
            "id": "t2",
            "type": "table",
            "content": "Quarter | Revenue | Operating Expenses\nQ1 | $10M | Refer to Table 4 for 2023",
            "page_number": 5,
        },
    ]
    layout = extract_layout_from_elements(elements, total_pages=10, document_id="doc_unanchored")

    # Table 4 must NOT be in definitions
    assert "table 4" not in layout.definitions
    # Table 4 MUST be in cross-references pointing to page 5
    assert "table 4" in layout.cross_references
    assert layout.cross_references["table 4"][0].page_number == 5


def test_pypdf_outline_page_range_bounded_by_subsequent_sections(tmp_path):
    """Verify pypdf bookmark extraction bounds section page_end by subsequent section start."""
    from pypdf import PdfWriter
    from src.ingestion.page_index_service import extract_pypdf_outline

    writer = PdfWriter()
    # Add 5 blank pages
    for _ in range(5):
        writer.add_blank_page(width=100, height=100)

    # Outline bookmarks: Chapter 1 at page 1 (0-indexed 0), Chapter 2 at page 3 (0-indexed 2)
    writer.add_outline_item("Chapter 1: Intro", page_number=0)
    writer.add_outline_item("Chapter 2: Methods", page_number=2)

    pdf_file = tmp_path / "multi_chapter.pdf"
    with open(pdf_file, "wb") as f:
        writer.write(f)

    outline_items, total_pages = extract_pypdf_outline(pdf_file)
    assert total_pages == 5
    assert len(outline_items) == 2

    ch1_title, ch1_level, ch1_start, ch1_end = outline_items[0]
    assert ch1_title == "Chapter 1: Intro"
    assert ch1_start == 1
    assert ch1_end == 2  # Bounded by Chapter 2 starting on page 3

    ch2_title, ch2_level, ch2_start, ch2_end = outline_items[1]
    assert ch2_title == "Chapter 2: Methods"
    assert ch2_start == 3
    assert ch2_end == 5  # Last section extends to document end


def test_cross_reference_heavy_payload_sizing_and_overflow_bound():
    """Verify complete summary sizing includes cross-references, triggering overflow and bounding persisted summary."""
    import json
    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    artifact_store = S3PageIndexArtifactStore()
    pi_repo = PageIndexRepository(boundary_repo=boundary_repo, artifact_store=artifact_store)

    # 1. Elements with few definitions (small definition payload ~500 bytes)
    elements = [
        {"id": "elem_def_1", "type": "table", "content": "Table 1: Tiny definition", "page_number": 1},
        {"id": "elem_def_2", "type": "figure", "content": "Figure 1: Another definition", "page_number": 2},
    ]

    # 2. Add 200 textual cross-references with distinct table labels across pages
    for i in range(1, 201):
        elements.append({
            "id": f"elem_cr_{i}",
            "type": "paragraph",
            "content": f"As observed in the experimental section, see Table {i} for details.",
            "page_number": i,
        })

    layout = extract_layout_from_elements(elements, total_pages=200, document_id="doc_cr_heavy")
    assert len(layout.definitions) >= 2
    assert len(layout.cross_references) >= 200

    # Set threshold to 3000 bytes.
    # Definitions alone fit in ~500 bytes.
    # Complete payload with 200 cross-references exceeds 4500 bytes (> 3000 threshold).
    threshold = 3000
    summary = pi_repo.persist_manifest(
        "ws_test", "doc_cr_heavy", "v1", layout, threshold_bytes=threshold
    )

    # S3 overflow MUST be triggered because complete sizing includes cross-references!
    assert summary.has_s3_overflow is True
    assert summary.s3_overflow_key is not None
    assert len(summary.inline_definitions) == 0
    assert len(summary.inline_cross_references) == 0

    # 100% of all cross-references are preserved in S3 detail without silent truncation
    detail = pi_repo.get_detail(summary)
    assert detail is not None
    assert "table 1" in detail.cross_references
    assert "table 200" in detail.cross_references
    assert len(detail.cross_references) == 200

    # The persisted summary in DynamoDB / boundary repo is strictly bounded within threshold
    saved_summary = boundary_repo.get_page_index_summary("ws_test", "doc_cr_heavy", "v1")
    assert saved_summary is not None
    serialized_bytes = len(json.dumps(saved_summary.model_dump()).encode("utf-8"))
    assert serialized_bytes <= threshold
    assert summary.raw_payload_bytes <= threshold


def test_sync_concurrency_controller_admission_deadlines_and_capacity_retention():
    """Verify synchronous concurrency controller bounds admission, enforces deadlines, and retains worker capacity."""
    from src.services.retrieval.page_index_lookup import PageIndexConcurrencyController

    # Controller with capacity 1 and queue 1
    controller = PageIndexConcurrencyController(max_concurrency=1, max_queue=1)
    thread_finished = False

    def lingering_storage_worker(*args):
        nonlocal thread_finished
        time.sleep(0.08)
        thread_finished = True
        return "storage_result"

    # 1. Caller timeout test: worker takes 80ms, caller deadline is 20ms
    t0 = time.perf_counter()
    res, status = controller.run_storage_work_sync(lingering_storage_worker, deadline_s=0.020)
    elapsed = time.perf_counter() - t0

    # Caller receives timeout promptly
    assert status == "caller_timeout"
    assert res is None
    assert elapsed < 0.040

    # Worker capacity remains retained until underlying thread actually finishes!
    assert controller.active_count == 1
    assert thread_finished is False

    # Wait for the lingering thread to finish
    time.sleep(0.08)
    assert thread_finished is True
    assert controller.active_count == 0

    # 2. Admission queue saturation test: capacity 1, queue 1
    # Occupy slot with a thread
    import concurrent.futures
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def slow_task(*args):
        time.sleep(0.1)
        return "ok"

    f1 = pool.submit(controller.run_storage_work_sync, slow_task, deadline_s=0.3)
    time.sleep(0.01)  # f1 acquires slot

    f2 = pool.submit(controller.run_storage_work_sync, slow_task, deadline_s=0.3)
    time.sleep(0.01)  # f2 enters admission queue

    # Third caller arrives when queue is full: rejected immediately!
    r3, s3 = controller.run_storage_work_sync(slow_task, deadline_s=0.3)
    assert s3 == "admission_rejected"
    assert r3 is None

    pool.shutdown(wait=True)

