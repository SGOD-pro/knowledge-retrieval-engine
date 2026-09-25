"""Unit tests for the 6 decoupled evidence retrieval strategies."""

import pytest
from unittest.mock import MagicMock, patch

from schemas.models import Chunk
from services.retrieval.evidence_contract import RetrievalLimits, RetrievalStrategyResult
from services.telemetry import RequestTelemetry, init_request_telemetry
from services.retrieval.strategies.vector_rerank import VectorRerankStrategy
from services.retrieval.strategies.bm25 import LexicalBM25Strategy
from services.retrieval.strategies.structured_table import StructuredTableStrategy
from services.retrieval.strategies.page_index import PageIndexStrategy
from services.retrieval.strategies.knowledge_graph import KnowledgeGraphStrategy
from services.retrieval.strategies.okf import OKFStrategy


@pytest.fixture
def sample_chunks():
    return [
        Chunk(
            id="c1",
            document_id="doc_a",
            source_format="pdf",
            text="Net sales for the quarter were $100 million.",
            element_type="paragraph",
            page_number=1,
            section_path=("Financials", "Overview"),
            location_reference="Page 1",
            metadata={"filename": "doc_a.pdf"},
        ),
        Chunk(
            id="c2",
            document_id="doc_b",
            source_format="pdf",
            text="Operating expenses reached $45 million.",
            element_type="paragraph",
            page_number=4,
            section_path=("Financials", "Expenses"),
            location_reference="Page 4",
            metadata={"filename": "doc_b.pdf"},
        ),
    ]


@pytest.mark.anyio
async def test_vector_rerank_strategy_records_remote_calls(sample_chunks):
    """Vector strategy records embedding and reranker remote calls."""
    telemetry = init_request_telemetry()
    mock_repo = MagicMock()
    mock_repo.generate_query_embedding.return_value = [0.1] * 1536
    mock_repo.search_vector.return_value = [(sample_chunks[0], 0.85), (sample_chunks[1], 0.70)]

    with patch("services.retrieval.strategies.vector_rerank.rerank") as mock_rerank:
        from dataclasses import replace
        scored_c1 = replace(sample_chunks[0], reranker_score=0.92)
        mock_rerank.return_value = [scored_c1]

        strategy = VectorRerankStrategy(repository=mock_repo)
        result = await strategy.retrieve(
            query="What were the net sales?",
            workspace_id="ws_test",
            limits=RetrievalLimits(top_k=5),
            telemetry=telemetry,
        )

        assert isinstance(result, RetrievalStrategyResult)
        assert result.strategy_name == "vector_rerank"
        assert len(result.items) == 1
        assert result.items[0].evidence_id == "c1"
        assert result.items[0].source_type == "chunk"
        assert result.items[0].strategy == "vector_rerank"

        # Telemetry: records embedding and reranker calls
        assert result.remote_call_counts["bedrock_embedding_calls"] >= 1
        assert result.remote_call_counts["reranker_remote_calls"] >= 1

        # Debug trace exposes candidates
        assert "raw_vector_candidates" in result.debug_trace
        assert "reranked_candidates" in result.debug_trace
        assert "final_retained_evidence_ids" in result.debug_trace
        assert result.debug_trace["final_retained_evidence_ids"] == ["c1"]


@pytest.mark.anyio
async def test_bm25_strategy_uses_zero_remote_calls(sample_chunks):
    """BM25 strategy must make zero remote calls and expose matched terms, scores, and distribution."""
    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = sample_chunks

    strategy = LexicalBM25Strategy(repository=mock_repo)
    result = await strategy.retrieve(
        query="operating expenses quarter",
        workspace_id="ws_test",
        limits=RetrievalLimits(top_k=5),
    )

    assert isinstance(result, RetrievalStrategyResult)
    assert result.strategy_name == "bm25"
    assert len(result.items) > 0
    assert result.items[0].source_type == "chunk"
    assert result.items[0].strategy == "bm25"

    # Zero remote calls guaranteed
    assert result.remote_call_counts.get("bedrock_embedding_calls", 0) == 0
    assert result.remote_call_counts.get("reranker_remote_calls", 0) == 0
    assert result.remote_call_counts.get("generation_calls", 0) == 0

    # Debug trace requirements
    assert "matched_terms" in result.debug_trace
    assert "bm25_scores" in result.debug_trace
    assert "document_distribution" in result.debug_trace


@pytest.mark.anyio
async def test_structured_table_strategy_zero_llm_zero_embeddings():
    """Structured strategy uses zero LLM and zero embedding calls."""
    mock_store = MagicMock()
    mock_svc = MagicMock()
    from services.retrieval.structured_query_service import StructuredQueryResult
    mock_svc.execute.return_value = StructuredQueryResult(
        answer_text="Total value is 1200",
        operator="sum",
        target_column="sales",
        predicate_columns=["year"],
        predicate_values=["2025"],
        selection_count=5,
        total_rows_in_table=100,
        result_value=1200,
        secondary_value=None,
        row_ids=["r1", "r2"],
        row_indices=[0, 1],
        selection_hash="abcdef0123456789",
        schema_binding_confidence=1.0,
        overall_confidence=1.0,
        table_id="financials",
        document_id="doc_fin",
        document_version="v1",
        workspace_id="ws_test",
        unit="USD",
    )

    strategy = StructuredTableStrategy(store=mock_store, service=mock_svc)
    result = await strategy.retrieve(
        query="Total sales for year 2025",
        workspace_id="ws_test",
    )

    assert isinstance(result, RetrievalStrategyResult)
    assert result.strategy_name == "structured_table"
    assert len(result.items) == 1
    assert result.items[0].source_type == "table_row_set"
    assert result.items[0].structured_payload["selection_hash"] == "abcdef0123456789"
    assert result.items[0].strategy == "structured_table"

    # Zero remote / LLM / embedding calls
    assert result.remote_call_counts["bedrock_embedding_calls"] == 0
    assert result.remote_call_counts["generation_calls"] == 0
    assert result.remote_call_counts["reranker_remote_calls"] == 0


@pytest.mark.anyio
async def test_structured_table_strategy_fails_closed_on_incomplete():
    """Structured strategy fails closed on incomplete table coverage."""
    from services.retrieval.structured_query_service import IncompleteDataError
    mock_svc = MagicMock()
    mock_svc.execute.side_effect = IncompleteDataError(table_id="sales", manifest={"persisted": 10, "source": 20})

    strategy = StructuredTableStrategy(service=mock_svc)
    result = await strategy.retrieve(
        query="Total sales",
        workspace_id="ws_test",
    )

    assert len(result.items) == 0
    assert result.failure_reason == "incomplete_table_coverage"
    assert result.confidence == 0.0


@pytest.mark.anyio
async def test_page_index_strategy_records_retrieval_llm_calls(sample_chunks):
    """PageIndex strategy records retrieval LLM calls (0 if deterministic structural score)."""
    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = sample_chunks

    strategy = PageIndexStrategy(repository=mock_repo)
    result = await strategy.retrieve(
        query="Net sales quarter",
        workspace_id="ws_test",
        limits=RetrievalLimits(top_k=5),
    )

    assert isinstance(result, RetrievalStrategyResult)
    assert result.strategy_name == "page_index"
    assert len(result.items) > 0
    assert result.items[0].source_type in ("page_region", "chunk")
    assert result.items[0].strategy == "page_index"

    # Exposes retrieval_llm_calls
    assert "retrieval_llm_calls" in result.remote_call_counts
    assert result.remote_call_counts["retrieval_llm_calls"] == 0
    assert "retrieval_llm_calls_used" in result.debug_trace
    assert result.debug_trace["retrieval_llm_calls_used"] is False


@pytest.mark.anyio
async def test_knowledge_graph_strategy_returns_provenance(sample_chunks):
    """KG strategy returns linked evidence with graph provenance."""
    mock_repo = MagicMock()
    mock_repo.expand_graph.return_value = [
        {"source_entity": "Apple", "relation": "subsidiary", "target_entity": "Beats", "source_chunk_id": "c1"}
    ]
    mock_repo.get_all_chunks.return_value = sample_chunks

    with patch("services.retrieval.strategies.knowledge_graph.extract_entities", return_value=["Apple"]):
        strategy = KnowledgeGraphStrategy(repository=mock_repo)
        result = await strategy.retrieve(
            query="What is the relation between Apple and Beats?",
            workspace_id="ws_test",
            limits=RetrievalLimits(max_hops=2),
        )

        assert isinstance(result, RetrievalStrategyResult)
        assert result.strategy_name == "knowledge_graph"
        assert len(result.items) >= 1
        assert "seed_entities" in result.debug_trace
        assert result.debug_trace["seed_entities"] == ["Apple"]
        assert "expansion_depth" in result.debug_trace
        assert "linked_evidence_chunks" in result.debug_trace
        assert "graph_confidence" in result.debug_trace

        ev = result.items[0]
        assert ev.strategy == "knowledge_graph"
        assert "graph_relation" in ev.provenance or "graph" in ev.provenance or ev.source_type in ("kg_node", "kg_edge", "chunk")


@pytest.mark.anyio
async def test_okf_strategy_returns_compatible_evidence():
    """OKF strategy returns OKF-compatible evidence with provenance."""
    mock_repo = MagicMock()
    mock_repo.get_okf_properties.return_value = [
        {
            "entity": "Apple",
            "attribute": "Commission File Number",
            "value": "001-36743",
            "source_chunk_id": "chk_okf_1",
            "confidence": 1.0,
        }
    ]

    with patch("services.retrieval.strategies.okf.extract_entities", return_value=["Apple"]):
        strategy = OKFStrategy(repository=mock_repo)
        result = await strategy.retrieve(
            query="What is Apple's Commission File Number?",
            workspace_id="ws_test",
        )

        assert isinstance(result, RetrievalStrategyResult)
        assert result.strategy_name == "okf"
        assert len(result.items) == 1
        assert result.items[0].source_type == "okf_fact"
        assert result.items[0].strategy == "okf"
        assert result.items[0].provenance["storage_type"] == "OKF-compatible runtime storage"
        assert result.items[0].text == "Apple Commission File Number: 001-36743"
