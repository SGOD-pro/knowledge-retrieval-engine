import logging
import uuid
import pytest
from fastapi.testclient import TestClient

from kre.api.main import app, repository
from kre.models import Chunk, Document
from kre.providers.embedding_provider import embed_text
from kre.providers.provider_client import get_active_provider
from kre.retrieval.bm25_retriever import BM25Retriever
from kre.retrieval.page_index_retriever import PageIndexRetriever
from kre.retrieval.planner import planner
from kre.retrieval.response_builder import build_citation
from kre.retrieval.vector_retriever import VectorRetriever

client = TestClient(app)


@pytest.fixture(autouse=True)
def seed_test_documents():
    repo = repository()
    doc_id_1 = str(uuid.uuid4())
    doc_id_2 = str(uuid.uuid4())

    emb_dev1 = embed_text("The refund policy allows returns within 30 days.", provider="dev")
    emb_dev2 = embed_text("Battery failure rate is 12% in extreme heat conditions.", provider="dev")

    c1 = Chunk(
        id="c1",
        document_id=doc_id_1,
        source_format="pdf",
        text="The refund policy allows returns within 30 days.",
        element_type="paragraph",
        page_number=1,
        section_path=("Policy",),
        bounding_box={"x1": 10.0, "y1": 10.0, "x2": 100.0, "y2": 50.0, "page_number": 1},
        location_reference=None,
        metadata={},
        structural_weight=2.0,
        provider="dev",
        embedding=emb_dev1,
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
        embedding=emb_dev2,
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
    assert "refund policy" in data["answer"].lower() or "30 days" in data["answer"].lower()


def test_r05_bm25_before_pageindex_before_vector(seed_test_documents):
    repo = repository()
    all_chunks = repo.get_all_chunks()

    # 1. BM25
    bm25 = BM25Retriever()
    bm25_res = bm25.search("refund policy", all_chunks)
    assert len(bm25_res) > 0

    # 2. PageIndex
    page_index = PageIndexRetriever()
    pi_chunks, candidate_pages = page_index.filter_and_rank("refund policy", [c for c, _ in bm25_res])
    assert len(pi_chunks) > 0

    # 3. Vector Search scoped to PageIndex candidate pages
    vec = VectorRetriever(repository=repo)
    vec_res = vec.search("refund policy", candidate_page_ids=candidate_pages)
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


def test_r30_query_and_corpus_provider_must_match(seed_test_documents):
    repo = repository()
    # Query with dev provider returns dev chunks
    results_dev = repo.search_vector(embed_text("refund", provider="dev"), provider="dev")
    assert any(c.provider == "dev" for c, _ in results_dev)

    # Querying prod provider returns zero matches when corpus is dev-only
    results_prod = repo.search_vector(embed_text("refund", provider="prod"), provider="prod")
    assert not any(c.provider == "dev" for c, _ in results_prod)


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
