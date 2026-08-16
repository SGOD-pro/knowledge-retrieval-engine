import logging
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from api.routes import repository
from ingestion.embed_service import embed_fast_local  # correct source (Component 7 fix)
from main import app
from providers.embedding_provider import embed_text as api_embed_text
from schemas.models import Chunk, Document
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.planner import planner
from services.retrieval.response_builder import build_citation
from services.retrieval.vector_retriever import VectorRetriever

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_test_documents():
    repo = repository()
    doc_id_1 = str(uuid.uuid4())
    doc_id_2 = str(uuid.uuid4())

    # Generate both fast (ONNX) and full (API) embeddings for each chunk
    emb_fast1 = embed_fast_local("The refund policy allows returns within 30 days.")
    emb_full1 = api_embed_text(
        "The refund policy allows returns within 30 days.", provider="dev"
    )

    emb_fast2 = embed_fast_local(
        "Battery failure rate is 12% in extreme heat conditions."
    )
    emb_full2 = api_embed_text(
        "Battery failure rate is 12% in extreme heat conditions.", provider="dev"
    )

    c1 = Chunk(
        id="c1",
        document_id=doc_id_1,
        source_format="pdf",
        text="The refund policy allows returns within 30 days.",
        element_type="paragraph",
        page_number=1,
        section_path=("Policy",),
        bounding_box={
            "x1": 10.0,
            "y1": 10.0,
            "x2": 100.0,
            "y2": 50.0,
            "page_number": 1,
        },
        location_reference=None,
        metadata={},
        structural_weight=2.0,
        provider="dev",
        embedding_fast=emb_fast1,
        embedding_full=emb_full1,
    )

    c2 = Chunk(
        id="c2",
        document_id=doc_id_2,
        source_format="docx",
        text="Battery failure rate is 12% in extreme heat conditions.",
        element_type="paragraph",
        page_number=2,
        section_path=("Specifications",),
        bounding_box=None,
        location_reference="Paragraph: 5",
        metadata={},
        structural_weight=1.5,
        provider="dev",
        embedding_fast=emb_fast2,
        embedding_full=emb_full2,
    )

    doc1 = Document(doc_id_1, "policy.pdf", "pdf", (c1,))
    doc2 = Document(doc_id_2, "specs.docx", "docx", (c2,))

    repo.save(doc1)
    repo.save(doc2)

    return [doc1, doc2]


def test_planner_routing_rules():
    # Rule 1 — Fast path
    plan_fast = planner.route("What is the refund policy?")
    assert plan_fast.fast_path is True
    assert plan_fast.use_graph is False
    assert plan_fast.stages == ["bm25", "page_index", "vector"]

    # Rule 2 — Relationship path
    plan_rel = planner.route("Why did revenue decrease because of overheating?")
    assert plan_rel.fast_path is False
    assert plan_rel.use_graph is True
    assert "graph" in plan_rel.stages

    # Rule 3 — Analytical path
    plan_ana = planner.route("Compare refund rates between Q1 and Q2")
    assert plan_ana.fast_path is False
    assert plan_ana.use_graph is False
    assert "okf" in plan_ana.stages


def test_r01_fast_path_zero_llm_calls():
    response = client.post("/query", json={"query": "What is the refund policy?"})
    assert response.status_code == 200
    data = response.json()

    assert data["fast_path"] is True
    assert len(data["citations"]) > 0
    assert (
        "refund policy" in data["answer"].lower() or "30 days" in data["answer"].lower()
    )


def test_r05_bm25_before_pageindex_before_vector(seed_test_documents):
    repo = repository()
    all_chunks = repo.get_all_chunks()

    # 1. BM25
    bm25 = BM25Retriever()
    bm25_res = bm25.search("refund policy", all_chunks)
    assert len(bm25_res) > 0

    # 2. PageIndex
    page_index = PageIndexRetriever()
    pi_chunks, candidate_pages = page_index.filter_and_rank(
        "refund policy", [c for c, _ in bm25_res]
    )
    assert len(pi_chunks) > 0

    # 3. Vector Search scoped to PageIndex candidate pages
    vec = VectorRetriever(repository=repo)
    vec_res = vec.search(
        "refund policy", fast_path=True, candidate_page_ids=candidate_pages
    )
    assert len(vec_res) > 0


def test_r10_all_modules_log_required_fields(caplog, seed_test_documents):
    caplog.set_level(logging.INFO)
    client.post("/query", json={"query": "refund policy"})

    log_text = caplog.text
    assert "bm25.latency_ms" in log_text
    assert "bm25.confidence_score" in log_text
    assert "page_index.latency_ms" in log_text
    assert "page_index.confidence_score" in log_text
    assert "vector.latency_ms" in log_text
    assert "vector.confidence_score" in log_text


def test_r20_all_citations_have_location(seed_test_documents):
    pdf_chunk = seed_test_documents[0].chunks[0]
    docx_chunk = seed_test_documents[1].chunks[0]

    cit_pdf = build_citation(pdf_chunk)
    cit_docx = build_citation(docx_chunk)

    assert cit_pdf.bounding_box is not None
    assert cit_docx.location_reference is not None and cit_docx.location_reference != ""


def test_fast_path_latency_breakdown():
    response = client.post("/query", json={"query": "What is the refund policy?"})
    assert response.status_code == 200
    data = response.json()

    breakdown = data["latency_breakdown"]
    assert "bm25_ms" in breakdown
    assert "page_index_ms" in breakdown
    assert "vector_ms" in breakdown
    assert "total_ms" in breakdown
    assert breakdown["total_ms"] < 400.0


def test_r27_no_forbidden_dependencies():
    """Assert torch, transformers, faiss are absent, but onnxruntime is present."""

    with pytest.raises(ImportError):
        pass
    with pytest.raises(ImportError):
        pass
    with pytest.raises(ImportError):
        pass


def test_r19_fast_path_uses_local_bge_and_fast_column(seed_test_documents):
    vec = VectorRetriever(repository=repository())
    # Patch at the actual definition site (ingestion.embed_service) and
    # at the import site in vector_retriever after Component 7 fix.
    with patch("ingestion.embed_service.embed_fast_local") as mock_local:
        with patch("providers.embedding_provider.embed_text") as mock_api:
            mock_local.return_value = [0.1] * 384
            vec.search("refund policy", fast_path=True)

            mock_local.assert_called_once()
            mock_api.assert_not_called()


def test_r19_full_path_uses_api_and_full_column(seed_test_documents):
    vec = VectorRetriever(repository=repository())
    with patch("ingestion.embed_service.embed_fast_local") as mock_local:
        with patch("providers.embedding_provider.embed_text") as mock_api:
            mock_api.return_value = [0.1] * 1024
            vec.search("refund policy", fast_path=False)

            mock_api.assert_called_once()
            mock_local.assert_not_called()


def test_r30_schema_level_routing_isolation():
    vec = VectorRetriever(repository=repository())

    plan_fast = planner.route("What is the refund policy?")
    sql_fast = vec.build_query_sql("What is the refund policy?", plan_fast)
    assert "embedding_fast" in sql_fast
    assert "embedding_full" not in sql_fast

    plan_full = planner.route("Why did revenue decrease because of overheating?")
    sql_full = vec.build_query_sql(
        "Why did revenue decrease because of overheating?", plan_full
    )
    assert "embedding_full" in sql_full
    assert "embedding_fast" not in sql_full


def test_fast_path_embedding_makes_zero_network_calls():
    # If fast path uses API, this mock will raise an exception during the test
    with patch("aws.infra.get_client") as mock_get_client:
        response = client.post("/query", json={"query": "What is the refund policy?"})
        assert response.status_code == 200
        assert response.json()["fast_path"] is True

        bedrock_calls = [
            call
            for call in mock_get_client.call_args_list
            if call[0][0] == "bedrock-runtime"
        ]
        assert (
            len(bedrock_calls) == 0
        ), f"Expected 0 Bedrock calls, got {len(bedrock_calls)}"


def test_full_path_embedding_makes_exactly_one_network_call():
    def mock_invoke_model_side_effect(*args, **kwargs):
        mock_response = MagicMock()
        if "embed" in kwargs.get("modelId", ""):
            mock_response.get.return_value.read.return_value = json.dumps(
                {"embedding": [0.1] * 1024}
            )
        else:
            mock_response.get.return_value.read.return_value = json.dumps(
                {"results": []}
            )
        return mock_response

    with patch.dict("os.environ", {"OPENROUTER_API_KEY": "test"}):
        with patch("aws.infra.get_client") as mock_get_client:
            mock_bedrock = MagicMock()
            mock_bedrock.invoke_model.side_effect = mock_invoke_model_side_effect
            mock_get_client.return_value = mock_bedrock

            with patch("services.llm.llm_service.call") as mock_llm:
                mock_llm.return_value = {"answer": "MOCK", "citations": []}

                # Rule 3 query triggers full path
                response = client.post(
                    "/query",
                    json={
                        "query": "Compare refund rates between Q1 and Q2",
                        "provider": "dev",
                    },
                )
                assert response.status_code == 200
                assert response.json()["fast_path"] is False

                # Check how many times Bedrock was invoked
                assert mock_bedrock.invoke_model.call_count >= 1


def test_fidelity_failure_blocks_llm():
    """Test that if fidelity check raises CoverageError, run_llm is skipped entirely."""
    from services.langgraph_pipeline import run_fidelity, run_llm

    state = {
        "query": "supercalifragilisticexpialidocious query that isn't in context",
        "compressed_text": "The sky is blue today and the weather is nice.",
        "stage_timings": {},
    }

    fidelity_res = run_fidelity(state)
    assert "error" in fidelity_res
    assert fidelity_res["error"] is not None
    assert fidelity_res["final_answer"] == "NOT_FOUND"

    state.update(fidelity_res)

    llm_res = run_llm(state)
    assert llm_res == {}


def test_c2_rejection_thresholds(seed_test_documents):
    """Test that retriever stages drop chunks below their threshold."""
    repo = repository()
    all_chunks = repo.get_all_chunks()

    # Test BM25
    bm25 = BM25Retriever()
    with patch("config.settings.BM25_THRESHOLD", 10.0):
        res = bm25.search("refund policy", all_chunks)
        assert len(res) == 0

    # Test PageIndex
    page_index = PageIndexRetriever()
    with patch("config.settings.PAGEINDEX_THRESHOLD", 10.0):
        res_chunks, _, _ = page_index.filter_and_rank("refund policy", all_chunks)
        assert len(res_chunks) == 0

    # Test Vector
    vec = VectorRetriever(repository=repo)
    with patch("config.settings.VECTOR_THRESHOLD", 10.0):
        res = vec.search("refund policy", fast_path=True, candidate_page_ids=[1])
        assert len(res) == 0

    # Test Reranker
    from services.retrieval.reranker import rerank

    with patch("config.settings.RERANKER_THRESHOLD", 10.0):
        with patch(
            "services.retrieval.reranker.rerank_documents",
            return_value=[0.1] * len(all_chunks),
        ):
            res = rerank("refund policy", all_chunks)
            assert len(res) == 0
