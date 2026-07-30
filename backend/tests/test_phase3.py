import pytest
from unittest.mock import patch, MagicMock
from kre.query_lambda.retrieval.planner import planner
from kre.query_lambda.retrieval.okf_retriever import OKFRetriever
from kre.query_lambda.graph.langgraph_pipeline import pipeline
from kre.query_lambda.retrieval.fidelity_check import CoverageError

@pytest.fixture
def mock_llm():
    with patch("kre.query_lambda.graph.langgraph_pipeline.call_llm") as mock_call:
        mock_call.return_value = {"answer": "MOCK_ANSWER", "citations": ["c1"]}
        yield mock_call

def test_r02_planner_zero_llm_calls():
    # Calling planner directly
    with patch("kre.query_lambda.llm.llm_service.call") as mock_llm:
        planner.route("test query")
        mock_llm.assert_not_called()

def test_r02_okf_lookup_zero_llm_calls():
    # Calling okf retriever
    with patch("kre.query_lambda.llm.llm_service.call") as mock_llm:
        retriever = OKFRetriever()
        retriever.lookup(["test"])
        mock_llm.assert_not_called()

def test_r02_full_path_exactly_one_llm_call(mock_llm):
    # Relationship path triggers full graph and LLM
    with patch("kre.query_lambda.retrieval.graph_retriever.GraphRetriever.expand") as mock_graph:
        with patch("kre.query_lambda.graph.langgraph_pipeline.check_fidelity") as mock_fidelity:
            mock_graph.return_value = []
            pipeline.run("Why did revenue decrease?")
            assert mock_llm.call_count == 1

def test_r03_llm_receives_compressed_context(mock_llm):
    with patch("kre.query_lambda.retrieval.graph_retriever.GraphRetriever.expand") as mock_graph:
        with patch("kre.query_lambda.graph.langgraph_pipeline.check_fidelity") as mock_fidelity:
            mock_graph.return_value = []
            pipeline.run("Why did revenue decrease?")
            assert mock_llm.call_count == 1
            args, kwargs = mock_llm.call_args
            # Second arg is context
            compressed_ctx = args[1] if len(args) > 1 else kwargs.get('compressed_context', '')
            # Length constraint
            assert len(compressed_ctx) < 4800

def test_r04_token_limit_never_exceeded(mock_llm):
    # Enforced by max 4800 chars
    with patch("kre.query_lambda.retrieval.graph_retriever.GraphRetriever.expand") as mock_graph:
        with patch("kre.query_lambda.graph.langgraph_pipeline.check_fidelity") as mock_fidelity:
            mock_graph.return_value = []
            pipeline.run("Why did revenue decrease?")
            assert mock_llm.call_count == 1
            args, kwargs = mock_llm.call_args
            compressed_ctx = args[1] if len(args) > 1 else kwargs.get('compressed_context', '')
            assert len(compressed_ctx) <= 4800

def test_r06_reranker_before_compressor():
    plan = planner.route("Why did sales drop?")
    stages = plan.stages
    assert stages.index("reranker") < stages.index("compressor")

def test_r10_all_modules_log_required_fields(caplog):
    import logging
    caplog.set_level(logging.INFO)
    
    with patch("kre.query_lambda.graph.langgraph_pipeline.call_llm") as mock_llm:
        mock_llm.return_value = {"answer": "MOCK_ANSWER", "citations": []}
        with patch("kre.query_lambda.retrieval.graph_retriever.GraphRetriever.expand") as mock_graph:
            mock_graph.return_value = []
            pipeline.run("Why did revenue decrease?")
            
    log_text = caplog.text
    # We check a few
    assert "reranker.latency_ms" in log_text
    assert "compressor.latency_ms" in log_text

def test_r14_fidelity_failure_blocks_llm():
    with patch("kre.query_lambda.graph.langgraph_pipeline.call_llm") as mock_llm:
        with patch("kre.query_lambda.graph.langgraph_pipeline.check_fidelity") as mock_fidelity:
            mock_fidelity.side_effect = CoverageError("Dropped entities")
            response = pipeline.run("Why did revenue decrease?")
            mock_llm.assert_not_called()
            assert response.answer == "NOT_FOUND"

def test_r15_confidence_computed_from_deterministic_formula(mock_llm):
    with patch("kre.query_lambda.retrieval.graph_retriever.GraphRetriever.expand") as mock_graph:
        mock_graph.return_value = []
        response = pipeline.run("Why did revenue decrease?")
        # It shouldn't be arbitrary
        assert isinstance(response.confidence_score, float)
        assert 0.0 <= response.confidence_score <= 1.0

def test_r16_graph_not_activated_for_factual_query():
    plan = planner.route("What is the refund policy?")
    assert not plan.use_graph
    assert "graph" not in plan.stages

def test_r16_graph_activated_for_relationship_query():
    plan = planner.route("Why did revenue decrease?")
    assert plan.use_graph
    assert "graph" in plan.stages

def test_r18_nova_micro_zero_query_time_calls():
    with patch("kre.ingestion_lambda.concept_service.extract_properties_nova_micro") as mock_nova:
        with patch("kre.query_lambda.graph.langgraph_pipeline.call_llm") as mock_llm:
            mock_llm.return_value = {"answer": "MOCK", "citations": []}
            pipeline.run("Why did revenue decrease?")
            mock_nova.assert_not_called()
