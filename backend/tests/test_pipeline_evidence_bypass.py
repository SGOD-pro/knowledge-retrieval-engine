"""Offline in-process pipeline regressions for fast_path evidence verification bypass."""

import json
from unittest.mock import patch

from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
from schemas.models import Chunk
from services.langgraph_pipeline import pipeline
from services.retrieval import bm25_retriever
from services.telemetry import get_request_telemetry, init_request_telemetry


class _StubRepo:
    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks

    def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
        return [c for c in self._chunks if not workspace_id or c.workspace_id == workspace_id]

    def search_vector(self, *args, **kwargs):
        return [(c, 0.9) for c in self._chunks]

    def search_vector_fast(self, *args, **kwargs):
        return [(c, 0.9) for c in self._chunks]

    def get(self, doc_id: str):
        return None


def _setup_workspace(chunks: list[Chunk]):
    ws_id = "ws_bypass_test"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Bypass Test", "usr_alice", workspace_id=ws_id)
    b_repo.create_source(ws_id, "ridgewood_clinic.csv", source_id="doc_ridgewood")
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc_ridgewood",
            source="ridgewood_clinic.csv",
            version="ver_ridgewood_01",
            format="csv",
            persisted_rows=len(chunks),
            total_rows=len(chunks),
            created_at="2026-11-28T00:00:00Z",
        )
    )
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    return ws_id, b_repo, _StubRepo(chunks)


def _run_pipeline(query: str, chunks: list[Chunk], llm_answer: str | None = None):
    ws_id, b_repo, repo = _setup_workspace(chunks)
    init_request_telemetry()
    llm_payload = json.dumps(
        {
            "answer": llm_answer or "I couldn't find any relevant passages in the workspace documents matching your query.",
            "citations": [chunks[0].id] if llm_answer and chunks else [],
        }
    )
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        patch("services.llm.llm_service.generate_completion", return_value=(llm_payload, {"input_tokens": 20, "output_tokens": 15})) as gen_mock,
    ):
        res = pipeline.run(
            query=query,
            document_ids=["doc_ridgewood"],
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
        )
    return res, get_request_telemetry(), gen_mock.call_count


def test_false_premise_with_contradictory_evidence_does_not_bypass_to_raw_fast_path():
    chunks = [
        Chunk(
            id="doc_ridgewood:csv:row1",
            document_id="doc_ridgewood",
            source_format="csv",
            text="District: Ridgewood. Birth registration (%): -92.5.",
            element_type="row",
            workspace_id="ws_bypass_test",
            version="ver_ridgewood_01",
            document_version="ver_ridgewood_01",
        )
    ]
    res, tel, gen_calls = _run_pipeline(
        "The clinic ledger says Ridgewood had 92.5% birth registration. Confirm whether this is accurate and state the stored value.",
        chunks,
        llm_answer="The claim of 92.5% is inaccurate; the stored birth registration value for Ridgewood is -92.5%.",
    )
    assert res.executed_path == "full"
    assert gen_calls == 1
    assert "-92.5" in res.answer and "inaccurate" in res.answer.lower()
    assert len(res.citations) == 1
    assert res.citations[0]["chunk_id"] == "doc_ridgewood:csv:row1"
    assert res.citations[0]["document_version"] == "ver_ridgewood_01"


def test_related_passage_cannot_answer_refuses_with_zero_citations():
    chunks = [
        Chunk(
            id="doc_ridgewood:csv:row1",
            document_id="doc_ridgewood",
            source_format="csv",
            text="District: Ridgewood. Birth registration (%): 94.0.",
            element_type="row",
            workspace_id="ws_bypass_test",
            version="ver_ridgewood_01",
            document_version="ver_ridgewood_01",
        )
    ]
    res, _, _ = _run_pipeline(
        "What is the pediatric ICU bed count in Ridgewood?",
        chunks,
        llm_answer=None,
    )
    assert "couldn't find" in res.answer.lower() or "not" in res.answer.lower()
    assert res.citations == []


def test_supported_direct_lookup_preserves_zero_llm_fast_path():
    chunks = [
        Chunk(
            id="doc_ridgewood:csv:row1",
            document_id="doc_ridgewood",
            source_format="csv",
            text="District: Ridgewood. Clinic beds: 48.",
            element_type="row",
            workspace_id="ws_bypass_test",
            version="ver_ridgewood_01",
            document_version="ver_ridgewood_01",
        )
    ]
    res, tel, gen_calls = _run_pipeline("Clinic beds for Ridgewood", chunks)
    assert res.executed_path == "fast"
    assert tel.generation_calls == 0 and gen_calls == 0
    assert "48" in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["chunk_id"] == "doc_ridgewood:csv:row1"
    assert res.citations[0]["document_version"] == "ver_ridgewood_01"


def test_forced_fast_relationship_comparison_with_real_extraction_does_not_emit_raw_value():
    from services.retrieval.extractor import extract_verified_fact
    from services.retrieval.planner import Plan, planner

    c = Chunk(
        id="doc_ridgewood:csv:row1",
        document_id="doc_ridgewood",
        source_format="csv",
        text="District: Ridgewood. Clinic beds: 48.",
        element_type="row",
        workspace_id="ws_bypass_test",
        version="ver_ridgewood_01",
        document_version="ver_ridgewood_01",
    )
    q = "How does Ridgewood Clinic beds compare with national standards?"
    ext_ans, ext_chunk = extract_verified_fact(q, [c])
    assert ext_ans == "48" and ext_chunk is not None and ext_chunk.id == c.id

    forced_plan = Plan(fast_path=True, use_graph=False, stages=["bm25", "page_index", "vector"])
    with patch.object(planner, "route", return_value=forced_plan):
        res, _, gen_calls = _run_pipeline(
            q,
            [c],
            llm_answer="Ridgewood has 48 clinic beds, which cannot be compared without national standard figures in the document.",
        )

    assert res.planned_path == "fast"
    assert res.executed_path == "full"
    assert gen_calls == 1
    assert "48" in res.answer and "compared" in res.answer.lower()
    assert len(res.citations) == 1
    assert res.citations[0]["chunk_id"] == "doc_ridgewood:csv:row1"
    assert res.citations[0]["document_version"] == "ver_ridgewood_01"
