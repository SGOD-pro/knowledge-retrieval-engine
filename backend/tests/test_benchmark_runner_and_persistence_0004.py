"""Test suite for reproducible benchmark runner and recoverable metric persistence (Spec 0004 AC-9, AC-10, AC-11).

Verifies:
1. S3 benchmark artifact storage and retrieval.
2. Recoverable persistence write order (S3 first, then DynamoDB).
3. Workspace boundary scoping and isolation of benchmark metadata across repositories.
4. Benchmark runner metrics calculation: correctness, evidence support, coverage, token usage, and latency.
"""

from decimal import Decimal
import io
import json
from unittest.mock import MagicMock, call, patch
import pytest

from src.schemas.contracts.boundary import (
    BenchmarkRunMetadata,
    CitationItem,
    QueryExecutionOutcome,
    StorageUnavailableError,
)
from src.schemas.contracts.location import DocumentLocation, TableLocation
from src.db.workspace_boundary_repo import (
    InMemoryWorkspaceBoundaryRepository,
    SQLiteWorkspaceBoundaryRepository,
    DynamoDBWorkspaceBoundaryRepository,
)
from services.benchmark.artifact_store import S3BenchmarkArtifactStore
from services.benchmark.persistence_service import BenchmarkPersistenceService
from services.benchmark.benchmark_runner import (
    BenchmarkRunner,
    BenchmarkTestCase,
)


def test_benchmark_artifact_store_s3_read_write():
    """Verify S3BenchmarkArtifactStore uploads and retrieves JSON reports with standard formatting."""
    mock_s3 = MagicMock()
    store = S3BenchmarkArtifactStore(s3_client=mock_s3, bucket_name="test-benchmark-bucket")

    workspace_id = "ws_test_01"
    run_id = "bm_run_123"
    payload = {
        "benchmark_run_id": run_id,
        "summary": {"correctness_score": 0.95, "avg_latency_ms": 120.5},
        "queries": [{"query_id": "q1", "correct": True}],
    }

    # 1. Save artifact
    s3_key = store.save_artifact(workspace_id, run_id, payload)
    assert s3_key == "benchmarks/ws_test_01/bm_run_123.json"
    mock_s3.put_object.assert_called_once()
    args, kwargs = mock_s3.put_object.call_args
    assert kwargs["Bucket"] == "test-benchmark-bucket"
    assert kwargs["Key"] == s3_key
    assert kwargs["ContentType"] == "application/json"
    saved_body = json.loads(kwargs["Body"].decode("utf-8"))
    assert saved_body["summary"]["correctness_score"] == 0.95

    # 2. Get artifact
    mock_body = MagicMock()
    mock_body.read.return_value = json.dumps(payload).encode("utf-8")
    mock_s3.get_object.return_value = {"Body": mock_body}

    retrieved = store.get_artifact(s3_key)
    assert retrieved is not None
    assert retrieved["benchmark_run_id"] == run_id
    assert retrieved["summary"]["avg_latency_ms"] == 120.5


def test_recoverable_persistence_sequence_s3_first():
    """Verify AC-10 recoverable ordering: S3 artifact is persisted BEFORE DynamoDB metadata."""
    mock_repo = MagicMock()
    mock_store = MagicMock()
    mock_store.save_artifact.return_value = "benchmarks/ws_test/bm_001.json"

    service = BenchmarkPersistenceService(boundary_repo=mock_repo, artifact_store=mock_store)

    metadata = BenchmarkRunMetadata(
        benchmark_run_id="bm_001",
        workspace_id="ws_test",
        status="completed",
        created_at="2026-10-15T00:00:00Z",
        total_queries=10,
        scored_queries=10,
        correctness_score=1.0,
        evidence_support_score=1.0,
        coverage_score=1.0,
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        avg_latency_ms=45.2,
        p95_latency_ms=60.0,
        ingestion_time_ms=500.0,
        s3_artifact_key="",
    )
    detailed_artifact = {"summary": {"total_queries": 10}}

    # Case 1: Normal success — S3 called first, then repo
    persisted = service.persist_benchmark_run("ws_test", metadata, detailed_artifact)
    assert persisted.s3_artifact_key == "benchmarks/ws_test/bm_001.json"
    mock_store.save_artifact.assert_called_once_with(
        workspace_id="ws_test",
        benchmark_run_id="bm_001",
        artifact_payload=detailed_artifact,
    )
    mock_repo.save_benchmark_metadata.assert_called_once_with(persisted)

    # Case 2: S3 failure prevents DynamoDB metadata write
    mock_store.reset_mock()
    mock_repo.reset_mock()
    mock_store.save_artifact.side_effect = RuntimeError("S3 connection timeout")

    with pytest.raises(StorageUnavailableError, match="Failed to persist benchmark artifact to S3"):
        service.persist_benchmark_run("ws_test", metadata, detailed_artifact)

    mock_repo.save_benchmark_metadata.assert_not_called()

    # Case 3: DynamoDB failure after S3 success raises StorageUnavailableError (orphan S3 is acceptable)
    mock_store.reset_mock()
    mock_repo.reset_mock()
    mock_store.save_artifact.side_effect = None
    mock_store.save_artifact.return_value = "benchmarks/ws_test/bm_002.json"
    mock_repo.save_benchmark_metadata.side_effect = Exception("DynamoDB throttling")

    with pytest.raises(StorageUnavailableError, match="Failed to persist benchmark metadata to DynamoDB"):
        service.persist_benchmark_run("ws_test", metadata, detailed_artifact)

    mock_store.save_artifact.assert_called_once()
    mock_repo.save_benchmark_metadata.assert_called_once()


def test_inmemory_repository_benchmark_scoping():
    """Verify InMemory repository scopes benchmark metadata to workspaces."""
    repo = InMemoryWorkspaceBoundaryRepository()

    meta_a = BenchmarkRunMetadata(
        benchmark_run_id="bm_a_1",
        workspace_id="ws_alpha",
        status="completed",
        created_at="2026-10-15T00:00:00Z",
        total_queries=5,
        scored_queries=5,
        correctness_score=0.8,
        evidence_support_score=1.0,
        coverage_score=1.0,
        prompt_tokens=50,
        completion_tokens=25,
        total_tokens=75,
        avg_latency_ms=30.0,
        p95_latency_ms=45.0,
        ingestion_time_ms=100.0,
        s3_artifact_key="benchmarks/ws_alpha/bm_a_1.json",
    )
    meta_b = BenchmarkRunMetadata(
        benchmark_run_id="bm_b_1",
        workspace_id="ws_beta",
        status="completed",
        created_at="2026-10-15T00:01:00Z",
        total_queries=3,
        scored_queries=3,
        correctness_score=1.0,
        evidence_support_score=1.0,
        coverage_score=1.0,
        prompt_tokens=30,
        completion_tokens=15,
        total_tokens=45,
        avg_latency_ms=25.0,
        p95_latency_ms=35.0,
        ingestion_time_ms=80.0,
        s3_artifact_key="benchmarks/ws_beta/bm_b_1.json",
    )

    repo.save_benchmark_metadata(meta_a)
    repo.save_benchmark_metadata(meta_b)

    # Retrieval by id
    assert repo.get_benchmark_metadata("ws_alpha", "bm_a_1") == meta_a
    assert repo.get_benchmark_metadata("ws_alpha", "bm_b_1") is None
    assert repo.get_benchmark_metadata("ws_beta", "bm_b_1") == meta_b

    # List scoping
    list_a = repo.list_benchmark_metadata("ws_alpha")
    assert len(list_a) == 1
    assert list_a[0].benchmark_run_id == "bm_a_1"

    list_b = repo.list_benchmark_metadata("ws_beta")
    assert len(list_b) == 1
    assert list_b[0].benchmark_run_id == "bm_b_1"


def test_sqlite_repository_benchmark_scoping(tmp_path):
    """Verify SQLite repository persists and scopes benchmark metadata across workspaces."""
    db_file = tmp_path / "test_boundary.db"
    repo = SQLiteWorkspaceBoundaryRepository(db_path=db_file)

    meta = BenchmarkRunMetadata(
        benchmark_run_id="bm_sq_1",
        workspace_id="ws_sqlite",
        status="completed",
        created_at="2026-10-15T00:00:00Z",
        total_queries=12,
        scored_queries=12,
        correctness_score=0.9167,
        evidence_support_score=1.0,
        coverage_score=0.95,
        prompt_tokens=240,
        completion_tokens=110,
        total_tokens=350,
        avg_latency_ms=52.3,
        p95_latency_ms=88.1,
        ingestion_time_ms=420.0,
        s3_artifact_key="benchmarks/ws_sqlite/bm_sq_1.json",
    )

    repo.save_benchmark_metadata(meta)

    # Fetch back
    fetched = repo.get_benchmark_metadata("ws_sqlite", "bm_sq_1")
    assert fetched is not None
    assert fetched.benchmark_run_id == "bm_sq_1"
    assert fetched.workspace_id == "ws_sqlite"
    assert fetched.correctness_score == pytest.approx(0.9167, 0.0001)
    assert fetched.avg_latency_ms == pytest.approx(52.3, 0.01)
    assert fetched.s3_artifact_key == "benchmarks/ws_sqlite/bm_sq_1.json"

    # Cross-workspace check
    assert repo.get_benchmark_metadata("ws_other", "bm_sq_1") is None
    assert len(repo.list_benchmark_metadata("ws_sqlite")) == 1
    assert len(repo.list_benchmark_metadata("ws_other")) == 0


def test_dynamodb_repository_benchmark_scoping():
    """Verify DynamoDB repository formats keys and numeric attributes correctly."""
    mock_table = MagicMock()
    repo = DynamoDBWorkspaceBoundaryRepository(table_name="kre-table", dynamodb_resource=MagicMock())
    repo._table = mock_table

    meta = BenchmarkRunMetadata(
        benchmark_run_id="bm_dyn_1",
        workspace_id="ws_dynamo",
        status="completed",
        created_at="2026-10-15T00:00:00Z",
        total_queries=8,
        scored_queries=8,
        correctness_score=0.875,
        evidence_support_score=1.0,
        coverage_score=1.0,
        prompt_tokens=160,
        completion_tokens=80,
        total_tokens=240,
        avg_latency_ms=64.2,
        p95_latency_ms=95.0,
        ingestion_time_ms=300.0,
        s3_artifact_key="benchmarks/ws_dynamo/bm_dyn_1.json",
    )

    # 1. Save
    repo.save_benchmark_metadata(meta)
    mock_table.put_item.assert_called_once()
    put_args, put_kwargs = mock_table.put_item.call_args
    item = put_kwargs["Item"]
    assert item["PK"] == "WORKSPACE#ws_dynamo"
    assert item["SK"] == "BENCHMARK#bm_dyn_1"
    assert item["workspace_id"] == "ws_dynamo"
    assert isinstance(item["correctness_score"], Decimal)
    assert item["correctness_score"] == Decimal("0.875")

    # 2. Get
    mock_table.get_item.return_value = {
        "Item": {
            "workspace_id": "ws_dynamo",
            "benchmark_run_id": "bm_dyn_1",
            "status": "completed",
            "created_at": "2026-10-15T00:00:00Z",
            "total_queries": 8,
            "scored_queries": 8,
            "correctness_score": Decimal("0.875"),
            "evidence_support_score": Decimal("1.0"),
            "coverage_score": Decimal("1.0"),
            "prompt_tokens": 160,
            "completion_tokens": 80,
            "total_tokens": 240,
            "avg_latency_ms": Decimal("64.2"),
            "p95_latency_ms": Decimal("95.0"),
            "ingestion_time_ms": Decimal("300.0"),
            "s3_artifact_key": "benchmarks/ws_dynamo/bm_dyn_1.json",
        }
    }
    loaded = repo.get_benchmark_metadata("ws_dynamo", "bm_dyn_1")
    assert loaded is not None
    assert loaded.benchmark_run_id == "bm_dyn_1"
    assert loaded.correctness_score == 0.875
    assert loaded.avg_latency_ms == 64.2


def test_reproducible_benchmark_runner_execution():
    """Verify BenchmarkRunner executes query test cases, computes metrics, and invokes persistence."""
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda workspace_id, metadata, detailed_artifact: metadata.model_copy(
        update={"s3_artifact_key": f"benchmarks/{workspace_id}/{metadata.benchmark_run_id}.json"}
    )

    # Define mock execution responses for 4 distinct query types
    def mock_executor(query: str, workspace_id: str):
        if "earliest" in query.lower():
            # Tabular aggregate query
            return QueryExecutionOutcome(
                execution_id="exec_1",
                status="SUCCESS",
                answer="The earliest year is 2005 and latest year is 2024.",
                citations=(
                    CitationItem(
                        evidence_id="ev_growth_1",
                        source_id="src_growth_csv",
                        location=TableLocation(table_id="growth_data", row_id=1),
                        snippet="2005 | 4.2%",
                        source_version=1,
                    ),
                ),
                surviving_source_ids=("src_growth_csv",),
                tombstoned_source_ids=(),
            )
        elif "pdf" in query.lower():
            # Text PDF query with page citation
            return QueryExecutionOutcome(
                execution_id="exec_2",
                status="SUCCESS",
                answer="The fund policy guidelines require diversified assets.",
                citations=(
                    CitationItem(
                        evidence_id="ev_handbook_1",
                        source_id="src_handbook_pdf",
                        location=DocumentLocation(page=1),
                        snippet="Diversified asset management standards.",
                        source_version=1,
                    ),
                ),
                surviving_source_ids=("src_handbook_pdf",),
                tombstoned_source_ids=(),
            )
        elif "tombstoned" in query.lower() or "incomplete" in query.lower():
            # Incomplete coverage query aborting aggregate claim
            return QueryExecutionOutcome(
                execution_id="exec_3",
                status="INSUFFICIENT_COVERAGE",
                answer="Aggregate calculation aborted: supporting source was tombstoned",
                citations=(),
                surviving_source_ids=(),
                tombstoned_source_ids=("src_deleted",),
            )
        else:
            # Insufficient evidence refusal
            return QueryExecutionOutcome(
                execution_id="exec_4",
                status="SUCCESS",
                answer="I couldn't find any relevant passages in the workspace documents matching your query.",
                citations=(),
                surviving_source_ids=(),
                tombstoned_source_ids=(),
            )

    runner = BenchmarkRunner(
        query_executor=mock_executor,
        persistence_service=mock_persistence,
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_1",
            query="What is the earliest and latest year in the dataset?",
            expected_answer="2005",
            query_type="tabular_aggregate",
        ),
        BenchmarkTestCase(
            query_id="tc_2",
            query="What does the pdf policy handbook state?",
            expected_pages=(1,),
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_3",
            query="Calculate the total metrics over incomplete dataset",
            query_type="partial_coverage",
            requires_complete_coverage=True,
        ),
        BenchmarkTestCase(
            query_id="tc_4",
            query="What is the unknown secret that is nowhere in the docs?",
            query_type="insufficient_evidence",
        ),
    ]

    report = runner.run_suite(
        workspace_id="ws_suite_test",
        test_cases=test_cases,
        ingestion_time_ms=1250.0,
        benchmark_run_id="bm_suite_001",
    )

    # Verify metrics
    metadata = report.metadata
    assert metadata.benchmark_run_id == "bm_suite_001"
    assert metadata.workspace_id == "ws_suite_test"
    assert metadata.total_queries == 4
    assert metadata.scored_queries == 4
    assert metadata.correctness_score == 1.0  # All 4 cases matched expectations
    assert metadata.evidence_support_score == 1.0  # Citations present when needed, empty on refusals
    assert metadata.coverage_score == 1.0  # Partial coverage correctly reported
    assert metadata.prompt_tokens > 0
    assert metadata.completion_tokens > 0
    assert metadata.avg_latency_ms >= 0.0
    assert metadata.ingestion_time_ms == 1250.0
    assert metadata.s3_artifact_key == "benchmarks/ws_suite_test/bm_suite_001.json"

    # Verify detailed artifact
    detailed = report.detailed_artifact
    assert detailed["benchmark_run_id"] == "bm_suite_001"
    assert len(detailed["queries"]) == 4
    assert detailed["queries"][0]["correctness"] is True
    assert detailed["queries"][2]["status"] == "INSUFFICIENT_COVERAGE"

    # Verify persistence call
    mock_persistence.persist_benchmark_run.assert_called_once()
