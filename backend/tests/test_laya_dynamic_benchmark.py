"""Offline in-process benchmark & held-out controls for dynamic session-aware LAYA routing."""

import json
from types import SimpleNamespace
from unittest.mock import patch

from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
from schemas.contracts.verification import VerificationRecord
from schemas.contracts.laya import BaselineEvidenceSummary, LayaEscalationRequest, MissingRequirementDescription
from schemas.models import Chunk
from services.langgraph_pipeline import pipeline
from services.retrieval import bm25_retriever
from services.retrieval.laya_service import LayaDecisionService


class _BenchRepo:
    def __init__(self, initial_chunks: list[Chunk], all_chunks: list[Chunk]):
        self._initial = initial_chunks
        self._all = all_chunks

    def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
        return [c for c in self._all if not workspace_id or c.workspace_id == workspace_id]

    def search_vector(self, *args, **kwargs):
        return [(c, 0.9) for c in self._initial]

    def search_vector_fast(self, *args, **kwargs):
        return [(c, 0.9) for c in self._initial]

    def get(self, doc_id: str):
        return None


class _StubVerifier:
    def verify(self, text: str, context: str, citations: list) -> VerificationRecord:
        return VerificationRecord(
            verification_id="vr_bench_01",
            workspace_id="ws_laya_dyn",
            query_id="q_bench_01",
            snapshot_id="snap_1",
            requirement_id="req_1",
            evidence_id="doc_r:page:4",
            verifier_version="v1.0",
            verifier_type="generative",
            attempt_number=1,
            execution_time_ms=10,
            support_status="ASSERTED",
            confidence_score=0.95,
            verification_rationale="Grounded in retrieved passage",
            verified_at="2026-11-28T00:00:00Z",
        )

    def is_ready(self) -> bool:
        return True


class _StubLayaClient:
    def __init__(self, payload: dict):
        self.payload = payload
        self.last_prompt: str | None = None

    def count_tokens(self, **kwargs):
        return {"inputTokens": 110}

    def converse(self, **kwargs):
        msgs = kwargs.get("messages") or []
        if msgs:
            self.last_prompt = str(msgs[0])
        return {
            "output": {"message": {"content": [{"text": json.dumps(self.payload)}]}},
            "usage": {"inputTokens": 110, "outputTokens": 40},
        }


def _setup_ws(chunks: list[Chunk], page_index_status: str | None = None):
    ws_id = "ws_laya_dyn"
    doc_id = chunks[0].document_id if chunks else "doc_r"
    src_name = "MO_Post_2024_25.csv" if doc_id != "doc_r" else "ridgewood.pdf"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Dyn LAYA", "usr_alice", workspace_id=ws_id)
    b_repo.create_source(ws_id, src_name, source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=src_name,
            version="ver_1",
            format="csv" if doc_id != "doc_r" else "pdf",
            persisted_rows=len(chunks),
            total_rows=len(chunks),
            page_index_status=page_index_status,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    return ws_id, b_repo


def test_supported_direct_lookup_passes_with_active_version_citation():
    c_clinic = Chunk(
        id="doc_r:csv:row1", document_id="doc_r", source_format="csv",
        text="District: Ridgewood. Clinic beds: 48.", element_type="row",
        workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
    )
    ws_id, b_repo = _setup_ws([c_clinic])
    repo = _BenchRepo([c_clinic], [c_clinic])
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
    ):
        res = pipeline.run(query="Clinic beds for Ridgewood", document_ids=["doc_r"], workspace_id=ws_id, benchmark_mode=True, boundary_repo=b_repo)
    assert "48" in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["chunk_id"] == "doc_r:csv:row1"
    assert res.citations[0]["document_version"] == "ver_1"


def test_negative_pediatric_icu_with_only_clinic_beds_refuses_zero_citations():
    c_clinic = Chunk(
        id="doc_r:csv:row1", document_id="doc_r", source_format="csv",
        text="District: Ridgewood. Clinic beds: 48.", element_type="row",
        workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
    )
    ws_id, b_repo = _setup_ws([c_clinic])
    repo = _BenchRepo([c_clinic], [c_clinic])

    def _fail_fid(q, ctx, **kw):
        from services.retrieval.fidelity_check import CoverageError
        raise CoverageError("No pediatric ICU evidence")

    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_fail_fid),
    ):
        res = pipeline.run(
            query="How many pediatric ICU beds are in Ridgewood?",
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
            deterministic_lexical_expansion_enabled=True,
        )
    assert res.answer == "NOT_FOUND"
    assert res.citations == []


def test_recoverable_pediatric_icu_recovers_via_laya_lexical_expansion_wiring():
    c_clinic = Chunk(
        id="doc_r:csv:row1", document_id="doc_r", source_format="csv",
        text="District: Ridgewood. Clinic beds: 48.", element_type="row",
        workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
    )
    c_icu = Chunk(
        id="doc_r:page:4", document_id="doc_r", source_format="pdf", page_number=4,
        text="Ridgewood pediatric ICU has 12 beds.", element_type="paragraph",
        workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
    )
    ws_id, b_repo = _setup_ws([c_clinic, c_icu])
    repo = _BenchRepo([c_clinic], [c_clinic, c_icu])

    fid_calls = [0]
    def _fid(q, ctx, **kw):
        fid_calls[0] += 1
        if fid_calls[0] == 1:
            from services.retrieval.fidelity_check import CoverageError
            raise CoverageError("Missing pediatric ICU beds")
        return 1.0

    laya_client = _StubLayaClient({
        "decision_id": "dec_icu",
        "route": "lexical_expansion",
        "parameters": {"route_type": "lexical_expansion", "query_terms": ["pediatric", "ICU", "beds"], "document_ids": ["doc_r"]},
        "confidence": 0.92,
        "rationale": "Search pediatric ICU beds",
    })
    llm_payload = json.dumps({"answer": "Ridgewood pediatric ICU has 12 beds.", "citations": ["doc_r:page:4"]})

    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        patch("services.langgraph_pipeline.call_llm", return_value={"answer": "Ridgewood pediatric ICU has 12 beds.", "citations": ["doc_r:page:4"], "usage": {"input_tokens": 20, "output_tokens": 10}}),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_fid),
    ):
        res = pipeline.run(
            query="How many pediatric ICU beds are in Ridgewood?",
            document_ids=["doc_r"],
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
            laya_enabled=True,
            laya_client=laya_client,
            verifier=_StubVerifier(),
        )
    assert res.laya_calls == 1
    assert "12" in res.answer
    assert [c["chunk_id"] for c in res.citations] == ["doc_r:page:4"]
    assert res.citations[0]["document_version"] == "ver_1"


def test_dynamic_payload_and_unavailable_graph_rejection():
    svc = LayaDecisionService(endpoint_url="https://laya.example/v1/systemone")
    req = LayaEscalationRequest(
        query_id="q_dyn",
        workspace_id="ws_laya_dyn",
        query="How does Ridgewood clinic capacity connect to ICU beds?",
        missing_requirement=MissingRequirementDescription(
            requirement_id="req_1",
            missing_dimension="relationship",
            description="Missing relationship between clinic capacity and ICU beds",
            failed_stage="run_fidelity_coverage",
            detected_gap="relationship_gap",
        ),
        available_routes=("lexical_expansion", "structural_expansion", "none"),
        retrieved_summary=(
            BaselineEvidenceSummary(
                chunk_id="doc_r:csv:row1", document_id="doc_r", document_version="ver_1",
                workspace_id="ws_laya_dyn", snippet="District: Ridgewood. Clinic beds: 48.",
                score=0.5, source_kind="text",
            ),
        ),
    )
    payload = svc._format_endpoint_payload(req)
    raw = json.dumps(payload)
    assert len(raw.encode("utf-8")) <= 980
    assert "complexity" not in payload["questions"]
    assert set(payload["questions"]["route"]["criteria"].keys()) == {"lexical_expansion", "structural_expansion", "none"}
    assert "Requirement:" in payload["state"]["document"]
    assert "Context:" in payload["state"]["document"]


def test_true_no_support_laya_none_returns_not_found_zero_citations():
    c_clinic = Chunk(
        id="doc_r:csv:row1", document_id="doc_r", source_format="csv",
        text="District: Ridgewood. Clinic beds: 48.", element_type="row",
        workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
    )
    ws_id, b_repo = _setup_ws([c_clinic])
    repo = _BenchRepo([c_clinic], [c_clinic])

    def _fail_fid(q, ctx, **kw):
        from services.retrieval.fidelity_check import CoverageError
        raise CoverageError("No helipad limit evidence")

    laya_client = _StubLayaClient({
        "decision_id": "dec_none",
        "route": "none",
        "parameters": {"route_type": "none"},
        "confidence": 0.95,
        "rationale": "Absent in corpus",
    })
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_fail_fid),
    ):
        res = pipeline.run(
            query="What is the helipad weight limit at Ridgewood?",
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
            laya_enabled=True,
            laya_client=laya_client,
            verifier=_StubVerifier(),
        )
    assert res.laya_calls == 1
    assert res.answer == "NOT_FOUND"
    assert res.citations == []


def test_nosup1_tabular_absent_entity_does_not_answer_from_other_system():
    """NOSUP-1 regression: absent tabular entity must refuse instead of citing another system's row, without forcing LAYA."""
    doc_id = "ee9fa513-fee8-54be-ba11-fc661c9ef680"
    rows = [
        Chunk(
            id=f"{doc_id}:csv:row2", document_id=doc_id, source_format="csv",
            text="Sl. No.: 1. SYSTEM: AYURVEDA. SANCTION: 685. FILLED: 521. VACANT: 164.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row3", document_id=doc_id, source_format="csv",
            text="Sl. No.: 2. SYSTEM: UNANI. SANCTION: 105. FILLED: 59. VACANT: 46.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
    ]
    ws_id, b_repo = _setup_ws(rows)
    repo = _BenchRepo(rows, rows)

    def _mock_rerank(q, chs, top_k=6, return_mode=False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    wrong_llm = {
        "answer": "The sanctioned medical officer count for SIDDHA is 685.",
        "citations": [f"{doc_id}:csv:row2"],
        "usage": {"input_tokens": 50, "output_tokens": 15},
    }
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
        patch("services.langgraph_pipeline.call_llm", return_value=wrong_llm),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
    ):
        res = pipeline.run(
            query="What is the sanctioned medical officer count for SIDDHA?",
            workspace_id=ws_id,
            boundary_repo=b_repo,
            laya_enabled=True,
            require_delivery_gate=False,
        )
    assert res.laya_calls == 0
    assert res.answer == "NOT_FOUND"
    assert res.citations == []


def test_nosup1_tabular_lowercase_and_wrong_row_citation_controls():
    """Tabular entity guard must catch lowercase entity names and unrelated row citations even when valid row is in context."""
    doc_id = "ee9fa513-fee8-54be-ba11-fc661c9ef680"
    rows = [
        Chunk(
            id=f"{doc_id}:csv:row2", document_id=doc_id, source_format="csv",
            text="Sl. No.: 1. SYSTEM: AYURVEDA. SANCTION: 685. FILLED: 521. VACANT: 164.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row6", document_id=doc_id, source_format="csv",
            text="Sl. No.: 5. SYSTEM: SIDDHA. SANCTION: 42. FILLED: 30. VACANT: 12.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
    ]
    ws_id, b_repo = _setup_ws(rows)
    repo = _BenchRepo(rows, rows)

    def _mock_rerank(q, chs, top_k=6, return_mode=False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    for query_text, cite_id, expect_ok in [
        ("What is the sanctioned medical officer count for siddha?", f"{doc_id}:csv:row2", False),
        ("What is the sanctioned medical officer count for SIDDHA?", f"{doc_id}:csv:row2", False),
        ("What is the sanctioned medical officer count for siddha?", f"{doc_id}:csv:row6", True),
    ]:
        llm_out = {
            "answer": "The sanctioned medical officer count is 42." if expect_ok else "The count is 685.",
            "citations": [cite_id],
            "usage": {"input_tokens": 50, "output_tokens": 15},
        }
        with (
            patch("services.langgraph_pipeline._get_repo", return_value=repo),
            patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
            patch("services.langgraph_pipeline.call_llm", return_value=llm_out),
            patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
            patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        ):
            res = pipeline.run(
                query=query_text,
                workspace_id=ws_id,
                boundary_repo=b_repo,
                laya_enabled=True,
                require_delivery_gate=False,
            )
        if expect_ok:
            assert res.answer == "The sanctioned medical officer count is 42."
            assert [c.get("chunk_id") for c in res.citations] == [f"{doc_id}:csv:row6"]
        else:
            assert res.answer == "NOT_FOUND"
            assert res.citations == []


def test_lex1_replaces_wrong_entity_citation_and_strips_extra_distractor():
    """LEX-1 citation regression: drop wrong-entity raw citations (even with same-number distractors) and cite only supporting active row."""
    doc_id = "ee9fa513-fee8-54be-ba11-fc661c9ef680"
    rows = [
        Chunk(
            id=f"{doc_id}:csv:row2", document_id=doc_id, source_format="csv",
            text="Sl. No.: 1. SYSTEM: AYURVEDA. SANCTION: 685. FILLED: 521. VACANT: 164.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row3", document_id=doc_id, source_format="csv",
            text="Sl. No.: 2. SYSTEM: UNANI. SANCTION: 105. FILLED: 59. VACANT: 46.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row4", document_id=doc_id, source_format="csv",
            text="Sl. No.: 3. SYSTEM: HOMEOPATHY. SANCTION: 81. FILLED: 47. VACANT: 34.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row_dist", document_id=doc_id, source_format="csv",
            text="Sl. No.: 9. SYSTEM: SIDDHA. SANCTION: 30. FILLED: 19. VACANT: 11.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row5", document_id=doc_id, source_format="csv",
            text="Sl. No.: 4. SYSTEM: NATURE CURE & YOGA. SANCTION: 30. FILLED: 19. VACANT: 11.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
    ]
    ws_id, b_repo = _setup_ws(rows)
    repo = _BenchRepo(rows, rows)

    def _mock_rerank(q, chs, top_k=6, return_mode=False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    # Case A: LLM answers 11 for NATURE CURE & YOGA but raw citation points to row4 (HOMEOPATHY)
    # Must replace row4 with row5 ONLY (ignoring row_dist which also has VACANT: 11).
    llm_wrong_cit = {
        "answer": "There are 11 vacant medical officer posts in NATURE CURE & YOGA.",
        "citations": [f"{doc_id}:csv:row4"],
        "usage": {"input_tokens": 50, "output_tokens": 15},
    }
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
        patch("services.langgraph_pipeline.call_llm", return_value=llm_wrong_cit),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
    ):
        res_a = pipeline.run(
            query="How many medical officer posts are vacant in NATURE CURE & YOGA?",
            workspace_id=ws_id,
            boundary_repo=b_repo,
            laya_enabled=False,
            require_delivery_gate=False,
        )
    assert res_a.synthesis_calls == 1
    assert "11" in res_a.answer
    assert [(c.get("chunk_id"), c.get("document_version")) for c in res_a.citations] == [
        (f"{doc_id}:csv:row5", "ver_1")
    ]

    # Case B: Legitimate multi-row citations (AYURVEDA and UNANI) must be preserved
    llm_multi = {
        "answer": "AYURVEDA has 164 vacant posts and UNANI has 46 vacant posts.",
        "citations": [f"{doc_id}:csv:row2", f"{doc_id}:csv:row3"],
        "usage": {"input_tokens": 50, "output_tokens": 15},
    }
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
        patch("services.langgraph_pipeline.call_llm", return_value=llm_multi),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
    ):
        res_b = pipeline.run(
            query="What are the vacant medical officer posts for AYURVEDA and UNANI?",
            workspace_id=ws_id,
            boundary_repo=b_repo,
            laya_enabled=False,
            require_delivery_gate=False,
        )
    assert [c.get("chunk_id") for c in res_b.citations] == [
        f"{doc_id}:csv:row2",
        f"{doc_id}:csv:row3",
    ]


def test_retained_tabular_citation_blocks_wrong_field_invented_number_and_swapped_values():
    """Retained tabular citations must reject wrong-field numbers, invented numbers, and swapped entity-value pairs."""
    doc_id = "ee9fa513-fee8-54be-ba11-fc661c9ef680"
    rows = [
        Chunk(
            id=f"{doc_id}:csv:row2", document_id=doc_id, source_format="csv",
            text="Sl. No.: 1. SYSTEM: AYURVEDA. SANCTION: 685. FILLED: 521. VACANT: 164.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row3", document_id=doc_id, source_format="csv",
            text="Sl. No.: 2. SYSTEM: UNANI. SANCTION: 105. FILLED: 59. VACANT: 46.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
        Chunk(
            id=f"{doc_id}:csv:row5", document_id=doc_id, source_format="csv",
            text="Sl. No.: 4. SYSTEM: NATURE CURE & YOGA. SANCTION: 30. FILLED: 19. VACANT: 11.",
            element_type="table_row", workspace_id="ws_laya_dyn", version="ver_1", document_version="ver_1",
        ),
    ]
    ws_id, b_repo = _setup_ws(rows)
    repo = _BenchRepo(rows, rows)

    def _mock_rerank(q, chs, top_k=6, return_mode=False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    cases = [
        # 1. Wrong field: 30 is SANCTION, not VACANT (11), while citing valid row5
        (
            "wrong_field",
            "How many medical officer posts are vacant in NATURE CURE & YOGA?",
            "There are 30 vacant medical officer posts in NATURE CURE & YOGA.",
            [f"{doc_id}:csv:row5"],
        ),
        # 2. Invented number: 99 is not in row5, while citing valid row5
        (
            "invented_number",
            "How many medical officer posts are vacant in NATURE CURE & YOGA?",
            "There are 99 vacant medical officer posts in NATURE CURE & YOGA.",
            [f"{doc_id}:csv:row5"],
        ),
        # 3. Swapped values: AYURVEDA (164) and UNANI (46) swapped, while citing valid row2 and row3
        (
            "swapped_values",
            "What are the vacant medical officer posts for AYURVEDA and UNANI?",
            "AYURVEDA has 46 vacant posts and UNANI has 164 vacant posts.",
            [f"{doc_id}:csv:row2", f"{doc_id}:csv:row3"],
        ),
    ]
    for label, q_text, ans_text, cids in cases:
        llm_out = {
            "answer": ans_text,
            "citations": cids,
            "usage": {"input_tokens": 50, "output_tokens": 15},
        }
        with (
            patch("services.langgraph_pipeline._get_repo", return_value=repo),
            patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
            patch("services.langgraph_pipeline.call_llm", return_value=llm_out),
            patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
            patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        ):
            res = pipeline.run(
                query=q_text,
                workspace_id=ws_id,
                boundary_repo=b_repo,
                laya_enabled=False,
                require_delivery_gate=False,
            )
        assert res.synthesis_calls == 1, f"{label} synth"
        assert res.answer == "NOT_FOUND", f"{label} leaked unsupported claim: {res.answer!r}"
        assert res.citations == [], f"{label} leaked citations: {res.citations}"
