from db.database import CloudRepository
from providers.embedding_provider import embed_text
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.vector_retriever import VectorRetriever


def test_qdrant_multiformat_filter_scoping():
    """Regression test verifying that Qdrant filters correctly scope candidates
    across both paginated (PDF) and pageless (DOCX, PPTX, CSV) documents without
    cross-document pollution.
    """
    test_queries = [
        {
            "format": "PDF",
            "doc": "1706.03762v7.pdf",
            "query": "How does Linear Transformer reduce memory and time complexity?",
            "min_vector_results": 1,
        },
        {
            "format": "DOCX",
            "doc": "Workflow Documentation.docx",
            "query": "What are the three architectural USPs of CodeCouncil?",
            "min_vector_results": 1,
        },
        {
            "format": "PPTX",
            "doc": "submission.pptx",
            "query": "What is the cost per citizen per month for WB Digital Sahayak?",
            "min_vector_results": 1,
        },
        {
            "format": "CSV",
            "doc": "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
            "query": "How many Assistant Professors are in Government Colleges?",
            "min_vector_results": 0,  # Tabular lookup routed via BM25/FastPath
        },
    ]

    repo = CloudRepository()
    all_chunks = repo.get_all_chunks()
    assert len(all_chunks) > 0, "No chunks loaded from repository"

    bm25 = BM25Retriever()
    page_index = PageIndexRetriever()
    vector_retriever = VectorRetriever(repository=repo)

    for item in test_queries:
        fmt = item["format"]
        query = item["query"]
        min_vec = item["min_vector_results"]

        # 1. BM25 stage
        bm25_results = bm25.search(query, all_chunks, top_k=20)
        bm25_chunks = [c for c, _ in bm25_results]
        assert len(bm25_chunks) > 0, f"BM25 returned 0 chunks for {fmt} query"

        # 2. PageIndex stage
        selected, cand_pages, cand_chunk_ids = page_index.filter_and_rank(
            query, bm25_chunks, top_k=10
        )
        assert len(selected) > 0, f"PageIndex returned 0 chunks for {fmt} query"

        # 3. Vector search stage
        query_emb = embed_text(query)
        vector_results = vector_retriever.search(
            query=query,
            query_embedding=query_emb,
            fast_path=False,
            candidate_page_ids=cand_pages,
            candidate_chunk_ids=cand_chunk_ids,
            top_k=10,
        )
        assert (
            len(vector_results) >= min_vec
        ), f"Vector search returned {len(vector_results)} < {min_vec} chunks for {fmt} query"
