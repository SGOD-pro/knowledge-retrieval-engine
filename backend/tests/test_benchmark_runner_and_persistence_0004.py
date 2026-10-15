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
                        snippet="The fund policy guidelines require diversified asset management standards.",
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


def test_adversarial_fabricated_answer_empty_citations_fails_insufficient_evidence():
    """Adversarial case: Fabricated answer with empty citations must FAIL insufficient-evidence scoring.
    
    Verifies that an engine hallucinating facts without citations on an unanswerable question
    is not credited for having 0 citations.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    # Engine fabricates positive assertion without refusal, but leaves citations empty
    def hallucinating_executor(query: str, workspace_id: str):
        return QueryExecutionOutcome(
            execution_id="exec_fab_1",
            status="SUCCESS",
            answer="The founder and CEO is John Doe and annual profits exceeded $50 million.",
            citations=(),
            surviving_source_ids=(),
            tombstoned_source_ids=(),
        )

    runner = BenchmarkRunner(query_executor=hallucinating_executor, persistence_service=mock_persistence)
    case = BenchmarkTestCase(
        query_id="adv_insufficient_1",
        query="What was the founder's secret personal recipe?",
        query_type="insufficient_evidence",
    )

    report = runner.run_suite(workspace_id="ws_adv_1", test_cases=[case])
    q_result = report.detailed_artifact["queries"][0]

    # Must FAIL correctness and evidence support
    assert q_result["correctness"] is False
    assert q_result["evidence_supported"] is False
    assert report.metadata.correctness_score == 0.0
    assert report.metadata.evidence_support_score == 0.0


def test_adversarial_wrong_source_or_version_citation_fails_evidence_support():
    """Adversarial case: Citations citing wrong source, wrong version, or unsupported items fail evidence support."""
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # Sub-case A: Wrong source ID
    responses["q_wrong_source"] = QueryExecutionOutcome(
        execution_id="exec_ws_1",
        status="SUCCESS",
        answer="The fund policy requires conservative leverage.",
        citations=(
            CitationItem(
                evidence_id="ev_01",
                source_id="src_unrelated_foreign_doc",  # Wrong source
                location=DocumentLocation(page=1),
                snippet="Conservative leverage requirement.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_unrelated_foreign_doc",),
        tombstoned_source_ids=(),
    )

    # Sub-case B: Wrong source version (stale or future version)
    responses["q_wrong_version"] = QueryExecutionOutcome(
        execution_id="exec_wv_1",
        status="SUCCESS",
        answer="The fund policy requires conservative leverage.",
        citations=(
            CitationItem(
                evidence_id="ev_02",
                source_id="src_policy_pdf",
                location=DocumentLocation(page=1),
                snippet="Conservative leverage requirement.",
                source_version=99,  # Wrong version (expected 1)
            ),
        ),
        surviving_source_ids=("src_policy_pdf",),
        tombstoned_source_ids=(),
    )

    # Sub-case C: Citation citing source version outside valid_source_versions
    responses["q_unbound_version"] = QueryExecutionOutcome(
        execution_id="exec_uv_1",
        status="SUCCESS",
        answer="Financial statement total assets.",
        citations=(
            CitationItem(
                evidence_id="ev_03",
                source_id="src_financials_csv",
                location=TableLocation(table_id="financials", row_id=5),
                snippet="Total Assets: 1000",
                source_version=1,  # Version 1 is outdated/unbound when active is 2
            ),
        ),
        surviving_source_ids=("src_financials_csv",),
        tombstoned_source_ids=(),
    )

    # Sub-case D: Supported citation with matching source and version
    responses["q_valid_evidence"] = QueryExecutionOutcome(
        execution_id="exec_valid_1",
        status="SUCCESS",
        answer="The fund policy requires conservative leverage.",
        citations=(
            CitationItem(
                evidence_id="ev_04",
                source_id="src_policy_pdf",
                location=DocumentLocation(page=1),
                snippet="Conservative leverage requirement.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_policy_pdf",),
        tombstoned_source_ids=(),
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_wrong_source",
            query="q_wrong_source",
            expected_answer="conservative leverage",
            expected_source_id="src_policy_pdf",
            expected_source_version=1,
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_wrong_version",
            query="q_wrong_version",
            expected_answer="conservative leverage",
            expected_source_id="src_policy_pdf",
            expected_source_version=1,
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_unbound_version",
            query="q_unbound_version",
            expected_answer="Total Assets",
            valid_source_versions={"src_financials_csv": 2},  # Only version 2 is valid
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_valid_evidence",
            query="q_valid_evidence",
            expected_answer="conservative leverage",
            expected_source_id="src_policy_pdf",
            expected_source_version=1,
            valid_source_versions={"src_policy_pdf": 1},
            query_type="text",
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_2", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Wrong source fails evidence support
    assert queries["tc_wrong_source"]["evidence_supported"] is False

    # Wrong version fails evidence support
    assert queries["tc_wrong_version"]["evidence_supported"] is False

    # Unbound / stale version fails evidence support
    assert queries["tc_unbound_version"]["evidence_supported"] is False

    # Matching source and version succeeds evidence support
    assert queries["tc_valid_evidence"]["evidence_supported"] is True


def test_adversarial_coverage_scoring_complete_vs_incomplete_datasets():
    """Adversarial case: Complete-dataset aggregate passes coverage; incomplete-dataset complete claim fails."""
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # 1. Valid complete-dataset aggregate: Clean data, successful aggregation
    responses["q_complete_aggregate"] = QueryExecutionOutcome(
        execution_id="exec_cov_1",
        status="SUCCESS",
        answer="The total revenue across all rows is $1,250,000.",
        citations=(
            CitationItem(
                evidence_id="ev_agg_1",
                source_id="src_sales_csv",
                location=TableLocation(table_id="sales", row_id=1),
                snippet="Total sum: 1250000",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_sales_csv",),
        tombstoned_source_ids=(),
    )

    # 2. Incomplete-dataset complete claim: Dataset has exclusions, but engine improperly claims SUCCESS
    responses["q_improper_complete_claim"] = QueryExecutionOutcome(
        execution_id="exec_cov_2",
        status="SUCCESS",
        answer="The total revenue across all rows is $950,000.",
        citations=(
            CitationItem(
                evidence_id="ev_agg_2",
                source_id="src_sales_corrupted_csv",
                location=TableLocation(table_id="sales", row_id=1),
                snippet="Total sum: 950000",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_sales_corrupted_csv",),
        tombstoned_source_ids=(),
    )

    # 3. Incomplete-dataset correctly blocked/refused
    responses["q_blocked_incomplete_claim"] = QueryExecutionOutcome(
        execution_id="exec_cov_3",
        status="INSUFFICIENT_COVERAGE",
        answer="Aggregate query blocked: source dataset contains incomplete coverage (2 malformed rows excluded).",
        citations=(),
        surviving_source_ids=(),
        tombstoned_source_ids=(),
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_valid_complete_aggregate",
            query="q_complete_aggregate",
            query_type="tabular_aggregate",
            requires_complete_coverage=True,
            dataset_is_incomplete=False,  # Complete dataset
        ),
        BenchmarkTestCase(
            query_id="tc_improper_claim_incomplete_data",
            query="q_improper_complete_claim",
            query_type="tabular_aggregate",
            requires_complete_coverage=True,
            dataset_is_incomplete=True,  # Incomplete dataset!
        ),
        BenchmarkTestCase(
            query_id="tc_properly_blocked_incomplete_data",
            query="q_blocked_incomplete_claim",
            query_type="partial_coverage",
            requires_complete_coverage=True,
            dataset_is_incomplete=True,  # Incomplete dataset!
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_3", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Valid complete-dataset aggregate MUST pass coverage scoring
    assert queries["tc_valid_complete_aggregate"]["coverage_valid"] is True

    # Incomplete-dataset complete claim MUST FAIL coverage scoring
    assert queries["tc_improper_claim_incomplete_data"]["coverage_valid"] is False

    # Incomplete-dataset blocked claim passes coverage scoring
    assert queries["tc_properly_blocked_incomplete_data"]["coverage_valid"] is True


def test_adversarial_token_accounting_and_provenance_distinction():
    """Adversarial case: Provider token usage is used when available, deterministic execution does not invent tokens,
    and unavailable vs estimated token usage is explicitly distinguished.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # Query 1: Provider token usage available
    res_provider = QueryExecutionOutcome(
        execution_id="exec_tok_1",
        status="SUCCESS",
        answer="Report on market risks and asset liquidity.",
        citations=(
            CitationItem(
                evidence_id="ev_tok_1",
                source_id="src_risk_doc",
                location=DocumentLocation(page=1),
                snippet="Market risks and liquidity factors.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_risk_doc",),
        tombstoned_source_ids=(),
        token_usage={"prompt_tokens": 128, "completion_tokens": 42},
    )
    responses["q_provider_tokens"] = res_provider

    # Query 2: Deterministic execution (tabular aggregate calculation, no model invoked)
    responses["q_deterministic_math"] = QueryExecutionOutcome(
        execution_id="exec_tok_2",
        status="SUCCESS",
        answer="Total sum is 5500.",
        citations=(
            CitationItem(
                evidence_id="ev_tok_2",
                source_id="src_math_csv",
                location=TableLocation(table_id="math", row_id=1),
                snippet="Sum: 5500",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_math_csv",),
        tombstoned_source_ids=(),
    )

    # Query 3: Text query where provider token usage is unavailable and estimation is disabled
    responses["q_unavailable_tokens"] = QueryExecutionOutcome(
        execution_id="exec_tok_3",
        status="SUCCESS",
        answer="Historical interest rate trajectories.",
        citations=(
            CitationItem(
                evidence_id="ev_tok_3",
                source_id="src_rates_doc",
                location=DocumentLocation(page=2),
                snippet="Interest rate changes from 2010 to 2020.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_rates_doc",),
        tombstoned_source_ids=(),
    )

    # Query 4: Text query where estimation is explicitly permitted as fallback
    responses["q_estimated_tokens"] = QueryExecutionOutcome(
        execution_id="exec_tok_4",
        status="SUCCESS",
        answer="Brief summary of portfolio allocations.",
        citations=(
            CitationItem(
                evidence_id="ev_tok_4",
                source_id="src_allocations_doc",
                location=DocumentLocation(page=1),
                snippet="Portfolio allocations across equity and debt.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_allocations_doc",),
        tombstoned_source_ids=(),
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_provider_reported",
            query="q_provider_tokens",
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_deterministic_zero",
            query="q_deterministic_math",
            query_type="tabular_aggregate",
        ),
        BenchmarkTestCase(
            query_id="tc_unavailable",
            query="q_unavailable_tokens",
            query_type="text",
            allow_token_estimation=False,
        ),
        BenchmarkTestCase(
            query_id="tc_estimated",
            query="q_estimated_tokens",
            query_type="text",
            allow_token_estimation=True,
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_tok", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # 1. Provider reported tokens used exactly
    assert queries["tc_provider_reported"]["prompt_tokens"] == 128
    assert queries["tc_provider_reported"]["completion_tokens"] == 42
    assert queries["tc_provider_reported"]["token_usage_source"] == "provider_reported"
    assert queries["tc_provider_reported"]["is_estimated_tokens"] is False

    # 2. Deterministic execution MUST NOT invent model usage (must be 0)
    assert queries["tc_deterministic_zero"]["prompt_tokens"] == 0
    assert queries["tc_deterministic_zero"]["completion_tokens"] == 0
    assert queries["tc_deterministic_zero"]["token_usage_source"] == "deterministic_zero"
    assert queries["tc_deterministic_zero"]["is_estimated_tokens"] is False

    # 3. Unavailable tokens must be marked unavailable without invented estimates
    assert queries["tc_unavailable"]["prompt_tokens"] == 0
    assert queries["tc_unavailable"]["completion_tokens"] == 0
    assert queries["tc_unavailable"]["token_usage_source"] == "unavailable"
    assert queries["tc_unavailable"]["is_estimated_tokens"] is False

    # 4. Estimated tokens explicitly identified as estimates
    assert queries["tc_estimated"]["prompt_tokens"] > 0
    assert queries["tc_estimated"]["completion_tokens"] > 0
    assert queries["tc_estimated"]["token_usage_source"] == "estimated"
    assert queries["tc_estimated"]["is_estimated_tokens"] is True

    # 5. Summary breakdown reflects exact categories
    breakdown = report.detailed_artifact["summary"]["token_usage_breakdown"]
    assert breakdown["provider_reported"] == 1
    assert breakdown["deterministic_zero"] == 1
    assert breakdown["unavailable"] == 1
    assert breakdown["estimated"] == 1


def test_adversarial_infrastructure_error_fails_partial_coverage_correctness_and_coverage():
    """Adversarial case: Infrastructure ERROR must fail partial-coverage correctness and coverage.

    Verifies that only agreed explicit coverage refusals earn credit;
    database or storage failures returning status="ERROR" or raising exceptions fail both.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # 1. Infrastructure failure returning status="ERROR" via adapter envelope/dict
    responses["q_error_status"] = {
        "execution_id": "exec_err_1",
        "status": "ERROR",
        "answer": "Execution error: DynamoDB service connection timed out",
        "citations": [],
        "token_usage": {"input_tokens": 0, "output_tokens": 0},
    }

    # 2. Proper explicit coverage refusal
    responses["q_proper_refusal"] = QueryExecutionOutcome(
        execution_id="exec_ref_1",
        status="INSUFFICIENT_COVERAGE",
        answer="Aggregate calculation aborted: supporting source was tombstoned",
        citations=(),
        surviving_source_ids=(),
        tombstoned_source_ids=("src_tombstoned",),
        token_usage={"input_tokens": 0, "output_tokens": 0},
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_error_must_fail",
            query="q_error_status",
            query_type="partial_coverage",
            requires_complete_coverage=True,
            dataset_is_incomplete=True,
        ),
        BenchmarkTestCase(
            query_id="tc_proper_refusal_must_pass",
            query="q_proper_refusal",
            query_type="partial_coverage",
            requires_complete_coverage=True,
            dataset_is_incomplete=True,
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_err", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Infrastructure ERROR MUST FAIL correctness and coverage
    assert queries["tc_error_must_fail"]["correctness"] is False
    assert queries["tc_error_must_fail"]["coverage_valid"] is False
    assert queries["tc_error_must_fail"]["evidence_supported"] is False

    # Agreed explicit coverage refusal MUST PASS correctness and coverage
    assert queries["tc_proper_refusal_must_pass"]["correctness"] is True
    assert queries["tc_proper_refusal_must_pass"]["coverage_valid"] is True
    assert queries["tc_proper_refusal_must_pass"]["evidence_supported"] is True


def test_adversarial_matching_citation_identifiers_alone_fails_without_evidence_support():
    """Adversarial case: Matching citation identifiers alone without factual support must FAIL evidence support.

    Verifies that an answer citing a valid source, version, and page but with unrelated context
    or ungrounded fabricated claims is rejected by existing evidence verification.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # Citations have valid identifier, source, and version, but snippet does NOT support the claim
    responses["q_unsupported_claim"] = QueryExecutionOutcome(
        execution_id="exec_unsup_1",
        status="SUCCESS",
        answer="The fund guarantees a 45% return annually with zero downside risk.",
        citations=(
            CitationItem(
                evidence_id="ev_valid_id",
                source_id="src_policy_pdf",
                location=DocumentLocation(page=1),
                snippet="General disclaimer: past performance is no guarantee of future results.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_policy_pdf",),
        tombstoned_source_ids=(),
    )

    # Valid supported claim
    responses["q_supported_claim"] = QueryExecutionOutcome(
        execution_id="exec_sup_1",
        status="SUCCESS",
        answer="General disclaimer: past performance is no guarantee of future results.",
        citations=(
            CitationItem(
                evidence_id="ev_valid_id_2",
                source_id="src_policy_pdf",
                location=DocumentLocation(page=1),
                snippet="General disclaimer: past performance is no guarantee of future results.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_policy_pdf",),
        tombstoned_source_ids=(),
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_unsupported_citation",
            query="q_unsupported_claim",
            expected_source_id="src_policy_pdf",
            expected_source_version=1,
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_supported_citation",
            query="q_supported_claim",
            expected_source_id="src_policy_pdf",
            expected_source_version=1,
            query_type="text",
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_cit", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Matching citation identifiers alone without factual support MUST FAIL
    assert queries["tc_unsupported_citation"]["evidence_supported"] is False

    # Supported citation passes
    assert queries["tc_supported_citation"]["evidence_supported"] is True


def test_production_execution_engine_token_trace_and_observed_preference():
    """Trace actual token usage from provider/query_service through ProductionExecutionEngine into BenchmarkRunner.

    Verifies that:
    1. ProductionExecutionEngine populates declared token_usage field on QueryExecutionOutcome.
    2. BenchmarkRunner observes provider reported usage without undeclared fields.
    3. Observed token usage overrules query-type assumptions.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    from src.api.boundary_adapters import ProductionExecutionEngine

    # Mock query_service to return observed token counts
    mock_query_service = MagicMock()
    mock_query_service.execute_query.return_value = {
        "answer": "Portfolio risk exposure across sectors.",
        "citations": [
            {
                "evidence_id": "ev_p1",
                "document_id": "doc_risk",
                "document_version": 1,
                "text": "Portfolio risk exposure details across sectors.",
                "page_number": 1,
            }
        ],
        "status": "success",
        "token_usage": {"input_tokens": 145, "output_tokens": 62},
        "document_ids": ["doc_risk"],
    }

    engine = ProductionExecutionEngine(query_service=mock_query_service)

    # 1. Execute query through production execution engine
    outcome = engine.execute_query(workspace_id="ws_prod_trace", query_text="What is portfolio risk exposure?")

    # Contract check: token_usage is a declared field on QueryExecutionOutcome
    assert isinstance(outcome, QueryExecutionOutcome)
    assert outcome.token_usage == {"input_tokens": 145, "output_tokens": 62}

    # 2. Run BenchmarkRunner with this execution engine
    runner = BenchmarkRunner(
        query_executor=lambda q, ws: outcome,
        persistence_service=mock_persistence,
    )
    case_text = BenchmarkTestCase(
        query_id="tc_traced_tokens",
        query="What is portfolio risk exposure?",
        query_type="text",
    )
    # Query with tabular_aggregate query_type but observed token usage from model
    case_agg_with_model = BenchmarkTestCase(
        query_id="tc_observed_overrules_assumption",
        query="Aggregate calculation using LLM synthesis",
        query_type="tabular_aggregate",
    )

    report = runner.run_suite(workspace_id="ws_prod_trace", test_cases=[case_text, case_agg_with_model])
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Traced tokens recorded accurately as provider_reported
    assert queries["tc_traced_tokens"]["prompt_tokens"] == 145
    assert queries["tc_traced_tokens"]["completion_tokens"] == 62
    assert queries["tc_traced_tokens"]["token_usage_source"] == "provider_reported"
    assert queries["tc_traced_tokens"]["is_estimated_tokens"] is False

    # Observed usage overrules query-type assumption
    assert queries["tc_observed_overrules_assumption"]["prompt_tokens"] == 145
    assert queries["tc_observed_overrules_assumption"]["completion_tokens"] == 62
    assert queries["tc_observed_overrules_assumption"]["token_usage_source"] == "provider_reported"


def test_generic_test_double_extracts_grounded_excerpts_without_pdf_specific_logic():
    """Verify that generic test double in test provider extracts grounded statements from context.

    Mocked generation check: uses generic lexical matching against prompt context
    without hardcoded PDF strings like "Mission:" or "wealth creator".
    Does not assert real model accuracy.
    """
    from providers.llm_provider import generate_completion

    # Case 1: Financial statements query and context
    user_prompt_fin = (
        "Context:\n"
        "[chunk_fin_1] Q3 Financial Highlights\n"
        "Net Operating Profit: $42.5 million for the quarter\n"
        "Operating Margin: Expanded by 180 basis points\n\n"
        "Query: What was the Net Operating Profit?"
    )
    raw_res, usage = generate_completion(
        system_prompt="Return JSON",
        user_prompt=user_prompt_fin,
        provider="test",
    )
    parsed = json.loads(raw_res)
    assert "42.5 million" in parsed["answer"]
    assert parsed["citations"] == ["chunk_fin_1"]
    assert usage["input_tokens"] > 0
    assert usage["output_tokens"] > 0

    # Case 2: Healthcare policy query and context
    user_prompt_health = (
        "Context:\n"
        "[chunk_med_2] Clinical Protocols 2026\n"
        "Emergency Admission Procedure: Immediate triage within 10 minutes of arrival\n"
        "Discharge Procedure: Physician sign-off required\n\n"
        "Query: What is the Emergency Admission Procedure?"
    )
    raw_res2, usage2 = generate_completion(
        system_prompt="Return JSON",
        user_prompt=user_prompt_health,
        provider="test",
    )
    parsed2 = json.loads(raw_res2)
    assert "10 minutes" in parsed2["answer"]
    assert parsed2["citations"] == ["chunk_med_2"]

    # Case 3: Empty context returns NOT_FOUND
    raw_res3, usage3 = generate_completion(
        system_prompt="Return JSON",
        user_prompt="Context:\n\nQuery: What is the secret password?",
        provider="test",
    )
    parsed3 = json.loads(raw_res3)
    assert parsed3["answer"] == "NOT_FOUND"
    assert parsed3["citations"] == []


def test_adversarial_year_matches_but_claimed_amount_contradicts_evidence():
    """Adversarial case: Year matches but claimed amount contradicts the evidence.

    Verifies that:
    1. An answer where the year matches (2024) but claimed amount (45%) contradicts
       the evidence (12%) FAILS evidence support. Matching year alone must not earn credit.
    2. An answer where the year and claimed amount match the evidence (12%) PASSES.
    3. Valid deterministic aggregate evidence handling is preserved.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    responses = {}

    def dynamic_executor(query: str, workspace_id: str):
        return responses[query]

    runner = BenchmarkRunner(query_executor=dynamic_executor, persistence_service=mock_persistence)

    # Subcase A: Year matches (2024), but claimed margin (45%) contradicts the evidence (12%)
    responses["q_contradicting_amount"] = QueryExecutionOutcome(
        execution_id="exec_contra_1",
        status="SUCCESS",
        answer="In 2024, operating margin expanded to 45%.",
        citations=(
            CitationItem(
                evidence_id="ev_contra_1",
                source_id="src_annual_report_pdf",
                location=DocumentLocation(page=1),
                snippet="In 2024, operating margin expanded to 12% across all business segments.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_annual_report_pdf",),
        tombstoned_source_ids=(),
    )

    # Subcase B: Supported claim where both year (2024) and amount (12%) match evidence
    responses["q_supported_amount"] = QueryExecutionOutcome(
        execution_id="exec_supp_1",
        status="SUCCESS",
        answer="In 2024, operating margin expanded to 12%.",
        citations=(
            CitationItem(
                evidence_id="ev_supp_1",
                source_id="src_annual_report_pdf",
                location=DocumentLocation(page=1),
                snippet="In 2024, operating margin expanded to 12% across all business segments.",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_annual_report_pdf",),
        tombstoned_source_ids=(),
    )

    # Subcase C: Valid deterministic aggregate query with table citation
    responses["q_valid_deterministic_aggregate"] = QueryExecutionOutcome(
        execution_id="exec_det_1",
        status="SUCCESS",
        answer="The total operating revenue is 154200.",
        citations=(
            CitationItem(
                evidence_id="ev_table_agg_1",
                source_id="src_financials_csv",
                location=TableLocation(table_id="financials_data", row_id="aggregated_rows"),
                snippet="Aggregated 250 rows from financials_data: sum=154200",
                source_version=1,
            ),
        ),
        surviving_source_ids=("src_financials_csv",),
        tombstoned_source_ids=(),
    )

    test_cases = [
        BenchmarkTestCase(
            query_id="tc_contradicting_amount",
            query="q_contradicting_amount",
            expected_source_id="src_annual_report_pdf",
            expected_source_version=1,
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_supported_amount",
            query="q_supported_amount",
            expected_source_id="src_annual_report_pdf",
            expected_source_version=1,
            query_type="text",
        ),
        BenchmarkTestCase(
            query_id="tc_valid_deterministic_aggregate",
            query="q_valid_deterministic_aggregate",
            expected_source_id="src_financials_csv",
            expected_source_version=1,
            query_type="tabular_aggregate",
        ),
    ]

    report = runner.run_suite(workspace_id="ws_adv_numeric", test_cases=test_cases)
    queries = {q["query_id"]: q for q in report.detailed_artifact["queries"]}

    # Contradicting amount MUST FAIL evidence support despite matching year
    assert queries["tc_contradicting_amount"]["evidence_supported"] is False

    # Supported amount PASSES
    assert queries["tc_supported_amount"]["evidence_supported"] is True

    # Valid deterministic aggregate evidence handling is preserved
    assert queries["tc_valid_deterministic_aggregate"]["evidence_supported"] is True


def test_adapter_to_runner_token_usage_regressions_missing_observed_zero_and_provider_reported():
    """Adapter-to-runner regressions: Missing, observed-zero, and provider-reported token usage.

    Verifies that:
    1. Missing token usage through ProductionExecutionEngine remains unavailable,
       not becoming deterministic_zero.
    2. Zero is reported only when execution telemetry establishes no model use.
    3. Provider-reported usage passes through directly.
    """
    mock_persistence = MagicMock()
    mock_persistence.persist_benchmark_run.side_effect = lambda *args, **kwargs: kwargs.get("metadata", args[1] if len(args) > 1 else None)

    from src.api.boundary_adapters import ProductionExecutionEngine

    mock_query_svc = MagicMock()
    engine = ProductionExecutionEngine(query_service=mock_query_svc)

    # 1. Missing token usage: query_service returns no token_usage key and no deterministic telemetry
    mock_query_svc.execute_query.return_value = {
        "answer": "General overview of liquidity metrics.",
        "citations": [
            {
                "evidence_id": "ev_liq_1",
                "document_id": "doc_liquidity",
                "document_version": 1,
                "text": "General overview of liquidity metrics.",
                "page_number": 1,
            }
        ],
        "status": "success",
        # token_usage omitted / absent
        "document_ids": ["doc_liquidity"],
    }

    outcome_missing = engine.execute_query(workspace_id="ws_adapter_tokens", query_text="Liquidity overview?")
    assert outcome_missing.token_usage is None

    runner_missing = BenchmarkRunner(
        query_executor=lambda q, ws: outcome_missing,
        persistence_service=mock_persistence,
    )
    case_missing_unavail = BenchmarkTestCase(
        query_id="tc_missing_unavail",
        query="Liquidity overview?",
        query_type="text",
        allow_token_estimation=False,
    )
    report_missing = runner_missing.run_suite(workspace_id="ws_adapter_tokens", test_cases=[case_missing_unavail])
    q_missing = report_missing.detailed_artifact["queries"][0]
    assert q_missing["token_usage_source"] == "unavailable"
    assert q_missing["prompt_tokens"] == 0
    assert q_missing["completion_tokens"] == 0
    assert q_missing["is_estimated_tokens"] is False

    # 2. Observed-zero usage: execution telemetry establishes no model use
    mock_query_svc.execute_query.return_value = {
        "answer": "The sum for amount is 4200.",
        "citations": [
            {
                "evidence_id": "ev_table_1",
                "table_id": "tbl_metrics",
                "document_version": 1,
                "text": "The sum for amount is 4200.",
                "evidence_type": "structured_aggregate",
            }
        ],
        "status": "success",
        "structured_aggregate": True,
        "token_usage": {"input_tokens": 0, "output_tokens": 0},
        "telemetry": {"generation_calls": 0},
        "document_ids": ["tbl_metrics"],
    }

    outcome_zero = engine.execute_query(workspace_id="ws_adapter_tokens", query_text="What is sum of amount?")
    assert outcome_zero.token_usage == {"input_tokens": 0, "output_tokens": 0}

    runner_zero = BenchmarkRunner(
        query_executor=lambda q, ws: outcome_zero,
        persistence_service=mock_persistence,
    )
    case_zero = BenchmarkTestCase(
        query_id="tc_observed_zero",
        query="What is sum of amount?",
        query_type="tabular_aggregate",
    )
    report_zero = runner_zero.run_suite(workspace_id="ws_adapter_tokens", test_cases=[case_zero])
    q_zero = report_zero.detailed_artifact["queries"][0]
    assert q_zero["token_usage_source"] == "deterministic_zero"
    assert q_zero["prompt_tokens"] == 0
    assert q_zero["completion_tokens"] == 0
    assert q_zero["is_estimated_tokens"] is False

    # 3. Provider-reported usage: query_service returns observed model usage
    mock_query_svc.execute_query.return_value = {
        "answer": "Detailed macroeconomic forecast narrative.",
        "citations": [
            {
                "evidence_id": "ev_macro_1",
                "document_id": "doc_macro",
                "document_version": 1,
                "text": "Detailed macroeconomic forecast narrative.",
                "page_number": 1,
            }
        ],
        "status": "success",
        "token_usage": {"input_tokens": 310, "output_tokens": 125},
        "document_ids": ["doc_macro"],
    }

    outcome_provider = engine.execute_query(workspace_id="ws_adapter_tokens", query_text="Macro forecast?")
    assert outcome_provider.token_usage == {"input_tokens": 310, "output_tokens": 125}

    runner_provider = BenchmarkRunner(
        query_executor=lambda q, ws: outcome_provider,
        persistence_service=mock_persistence,
    )
    case_provider = BenchmarkTestCase(
        query_id="tc_provider_usage",
        query="Macro forecast?",
        query_type="text",
    )
    report_provider = runner_provider.run_suite(workspace_id="ws_adapter_tokens", test_cases=[case_provider])
    q_provider = report_provider.detailed_artifact["queries"][0]
    assert q_provider["token_usage_source"] == "provider_reported"
    assert q_provider["prompt_tokens"] == 310
    assert q_provider["completion_tokens"] == 125
    assert q_provider["is_estimated_tokens"] is False
