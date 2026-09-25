"""Integration tests for strategy router within LangGraph pipeline."""

from unittest.mock import MagicMock, patch
import pytest

from schemas.models import Chunk
from services.langgraph_pipeline import pipeline
from services.retrieval.evidence_contract import EvidenceItem


@pytest.fixture
def mock_chunks():
    return [
        Chunk(
            id="c_sec_1",
            document_id="doc_sec",
            source_format="pdf",
            text="The Commission File Number of Apple Inc. is 001-36743.",
            element_type="paragraph",
            page_number=1,
            section_path=("Item 1", "General"),
            location_reference="Page 1",
            metadata={"filename": "SEC-Form-10Q.pdf"},
        ),
        Chunk(
            id="c_sec_2",
            document_id="doc_sec",
            source_format="pdf",
            text="Common stock outstanding was 14,594,180,000 shares.",
            element_type="paragraph",
            page_number=2,
            section_path=("Item 1", "Cover"),
            location_reference="Page 2",
            metadata={"filename": "SEC-Form-10Q.pdf"},
        ),
    ]


def test_pipeline_routes_through_strategy_router_and_preserves_strategy_in_citations(mock_chunks):
    """Router integration preserves strategy name in final citations and maps to retained evidence."""
    with patch("services.langgraph_pipeline._get_repo") as mock_get_repo:
        repo = MagicMock()
        repo.get_all_chunks.return_value = mock_chunks
        repo.search_vector.return_value = [(mock_chunks[0], 0.95)]
        mock_get_repo.return_value = repo

        with patch("services.retrieval.reranker.rerank", return_value=[mock_chunks[0]]):
            with patch("services.llm.llm_service.call") as mock_llm:
                mock_llm.return_value = {
                    "text": "The Commission File Number is 001-36743.",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                }

                resp = pipeline.run(
                    query="What is Apple's Commission File Number 001-36743?",
                    workspace_id="ws_test",
                )

                assert resp.status == "success"
                assert len(resp.citations) > 0
                for cit in resp.citations:
                    # Requirement 13: Router integration preserves strategy name in final citations
                    assert "strategy" in cit
                    assert cit["strategy"] in ("vector_rerank", "bm25", "page_index", "knowledge_graph", "okf", "structured_table")

                # Verify retained evidence items are present and matched
                assert hasattr(resp, "retained_evidence_items")
                assert len(resp.retained_evidence_items) > 0
                retained_ids = {ev.evidence_id for ev in resp.retained_evidence_items}
                for cit in resp.citations:
                    assert cit.get("chunk_id") in retained_ids or cit.get("table_id") in retained_ids


def test_pipeline_structured_aggregate_preserves_strategy_and_retained_evidence():
    """Structured queries preserve structured_table strategy in citation."""
    from services.retrieval.structured_query_service import StructuredQueryResult
    mock_result = StructuredQueryResult(
        answer_text="Total value was 100",
        operator="sum",
        target_column="Value_NZD",
        predicate_columns=["Year"],
        predicate_values=["2025"],
        selection_count=1,
        total_rows_in_table=60255,
        result_value=100,
        secondary_value=None,
        row_ids=["r1"],
        row_indices=[0],
        selection_hash="abcdef1234567890",
        schema_binding_confidence=1.0,
        overall_confidence=1.0,
        table_id="survay",
        document_id="doc_survay",
        document_version="v1",
        workspace_id="ws_test",
        unit="NZD",
    )

    with patch("services.retrieval.structured_query_service.StructuredQueryService.execute", return_value=mock_result):
        resp = pipeline.run(
            query="Total value of Value_NZD in survey dataset for year 2025",
            workspace_id="ws_test",
        )

        assert resp.status == "success"
        assert len(resp.citations) == 1
        cit = resp.citations[0]
        assert cit.get("evidence_type") == "structured_aggregate"
        assert cit.get("strategy") == "structured_table"
        assert cit.get("selection_hash") == "abcdef1234567890"
        assert hasattr(resp, "retained_evidence_items")
        assert len(resp.retained_evidence_items) == 1
        assert resp.retained_evidence_items[0].strategy == "structured_table"
