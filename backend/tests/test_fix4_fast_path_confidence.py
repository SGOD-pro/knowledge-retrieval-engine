import pytest
from schemas.models import Chunk
from services.langgraph_pipeline import end_fast_path

def test_fast_path_confidence_varies_with_match_strength():
    strong_chunk = Chunk(
        id="c_strong",
        document_id="doc1",
        text="The attention mechanism computes a weighted sum of values based on query key dot products.",
        source_format="pdf",
        page_number=1,
        element_type="paragraph",
        section_path="Root",
        similarity_score=0.94,
    )

    weak_chunk = Chunk(
        id="c_weak",
        document_id="doc2",
        text="Miscellaneous unrelated information regarding corporate travel expenses.",
        source_format="pdf",
        page_number=2,
        element_type="paragraph",
        section_path="Root",
        similarity_score=0.42,
    )

    # State with strong match
    state_strong = {
        "query": "What is the attention mechanism?",
        "top_chunks": [strong_chunk],
        "candidate_chunks": [strong_chunk],
        "stage_timings": {},
    }
    res_strong = end_fast_path(state_strong)

    # State with weak match
    state_weak = {
        "query": "What is the attention mechanism?",
        "top_chunks": [weak_chunk],
        "candidate_chunks": [weak_chunk],
        "stage_timings": {},
    }
    res_weak = end_fast_path(state_weak)

    # Assertions
    assert res_strong["confidence_score"] > 0.0
    assert res_weak["confidence_score"] > 0.0
    assert res_strong["confidence_score"] == 0.94
    assert res_weak["confidence_score"] == 0.42
    assert res_strong["confidence_score"] > res_weak["confidence_score"]
