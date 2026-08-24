from unittest.mock import patch

from schemas.models import Chunk
from src.db.database import CloudRepository
from src.services.retrieval.page_index_retriever import PageIndexRetriever
from src.services.retrieval.vector_retriever import VectorRetriever


def _make_chunk(cid: str, text: str, page_number: int | None, format: str = "docx"):
    return Chunk(
        id=cid,
        document_id="doc_test",
        source_format=format,
        text=text,
        element_type="paragraph",
        page_number=page_number,
        section_path=(),
        bounding_box=None,
        location_reference="p1",
        metadata={},
        structural_weight=1.0,
        provider="dev",
        embedding_fast=[0.1] * 384,
        embedding_full=[0.1] * 1024,
        image_s3_keys=(),
    )


def test_pageless_chunks_preserved_in_page_index_narrowing():
    """AE3: PageIndexRetriever must return pageless chunk IDs in candidate_chunk_ids."""
    c_pdf = _make_chunk("pdf_c1", "PDF content on page 5", page_number=5, format="pdf")
    c_docx = _make_chunk(
        "docx_c1", "DOCX content without page number", page_number=None, format="docx"
    )

    retriever = PageIndexRetriever()
    with patch(
        "src.services.retrieval.page_index_retriever.structural_score", return_value=1.0
    ):
        selected, pages, chunk_ids = retriever.filter_and_rank(
            "query", [c_pdf, c_docx], top_k=10
        )

    assert 5 in pages
    assert "docx_c1" in chunk_ids


def test_vector_search_includes_pageless_chunks_when_pages_present(monkeypatch):
    """AE3: VectorRetriever must search both candidate_page_ids and candidate_chunk_ids (OR logic)."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    c_pdf = _make_chunk("pdf_c1", "PDF content on page 5", page_number=5, format="pdf")
    c_docx = _make_chunk(
        "docx_c1", "DOCX content without page number", page_number=None, format="docx"
    )
    c_irrelevant = _make_chunk(
        "pdf_c2", "PDF content on page 99", page_number=99, format="pdf"
    )

    from schemas.models import Document

    repo = CloudRepository()
    doc = Document("doc_test", "test.docx", "docx", (c_pdf, c_docx, c_irrelevant))
    repo.save(doc)

    retriever = VectorRetriever(repository=repo)
    # Search with page constraint [5] AND chunk constraint ['docx_c1']
    results = retriever.search(
        query="query",
        query_embedding=[0.1] * 1024,
        fast_path=False,
        candidate_page_ids=[5],
        candidate_chunk_ids=["docx_c1"],
        top_k=10,
    )

    result_ids = [c.id for c, _ in results]
    assert "pdf_c1" in result_ids, "PDF chunk on candidate page 5 must be found"
    assert (
        "docx_c1" in result_ids
    ), "DOCX pageless chunk in candidate_chunk_ids must be found"
    assert (
        "pdf_c2" not in result_ids
    ), "Irrelevant PDF chunk on page 99 must be excluded"
