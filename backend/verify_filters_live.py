import json
import logging
from config import settings
from db.database import CloudRepository
from services.retrieval.bm25_retriever import BM25Retriever
from services.retrieval.page_index_retriever import PageIndexRetriever
from services.retrieval.vector_retriever import VectorRetriever
from providers.embedding_provider import embed_text

logging.basicConfig(level=logging.INFO)

test_queries = [
    {
        "format": "PDF",
        "doc": "1706.03762v7.pdf",
        "query": "How does Linear Transformer reduce memory and time complexity?"
    },
    {
        "format": "DOCX",
        "doc": "Workflow Documentation.docx",
        "query": "What are the three architectural USPs of CodeCouncil?"
    },
    {
        "format": "PPTX",
        "doc": "submission.pptx",
        "query": "What is the cost per citizen per month for WB Digital Sahayak?"
    },
    {
        "format": "CSV",
        "doc": "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
        "query": "How many Assistant Professors are in Government Colleges?"
    }
]

repo = CloudRepository()
all_chunks = repo.get_all_chunks()
bm25 = BM25Retriever()
page_index = PageIndexRetriever()
vector_retriever = VectorRetriever(repository=repo)

print(f"Loaded {len(all_chunks)} total chunks from CloudRepository.\n")

for item in test_queries:
    fmt = item["format"]
    query = item["query"]
    print("=" * 80)
    print(f"FORMAT: {fmt} | Query: {query}")
    print("=" * 80)
    
    # 1. BM25 stage
    bm25_results = bm25.search(query, all_chunks, top_k=20)
    bm25_chunks = [c for c, _ in bm25_results]
    formats_in_bm25 = set(c.source_format for c in bm25_chunks)
    print(f"[BM25] Returned {len(bm25_chunks)} candidates. Formats present: {formats_in_bm25}")
    
    # 2. PageIndex stage
    selected, cand_pages, cand_chunk_ids = page_index.filter_and_rank(query, bm25_chunks, top_k=10)
    print(f"[PageIndex] Filtered to {len(selected)} chunks.")
    print(f"  - candidate_page_ids: {cand_pages}")
    print(f"  - candidate_chunk_ids (count={len(cand_chunk_ids)}): {cand_chunk_ids[:5]}...")
    
    # 3. Construct Qdrant Filter directly to inspect raw structure
    from qdrant_client.http import models as qmodels
    must_filters = []
    should_filters = []
    if cand_pages:
        should_filters.append(qmodels.FieldCondition(
            key="page_number",
            match=qmodels.MatchAny(any=cand_pages)
        ))
    if cand_chunk_ids:
        should_filters.append(qmodels.FieldCondition(
            key="original_id",
            match=qmodels.MatchAny(any=cand_chunk_ids)
        ))
    if should_filters:
        must_filters.append(qmodels.Filter(should=should_filters))
    qfilter = qmodels.Filter(must=must_filters) if must_filters else None
    
    print(f"\n[Raw Qdrant Filter Object]:")
    if qfilter:
        print(qfilter.model_dump_json(indent=2))
    else:
        print("None (Unfiltered)")
        
    # 4. Execute VectorRetriever search
    query_emb = embed_text(query)
    vector_results = vector_retriever.search(
        query=query,
        query_embedding=query_emb,
        fast_path=False,
        candidate_page_ids=cand_pages,
        candidate_chunk_ids=cand_chunk_ids,
        top_k=10
    )
    
    print(f"\n[Vector Search Results]: {len(vector_results)} chunks returned")
    for rank, (chunk, score) in enumerate(vector_results[:5], 1):
        print(f"  {rank}. Doc: {chunk.document_id} | Format: {chunk.source_format} | Page: {chunk.page_number} | Score: {score:.4f} | ID: {chunk.id}")
    print()
