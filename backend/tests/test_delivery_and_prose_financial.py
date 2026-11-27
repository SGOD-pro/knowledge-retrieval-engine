"""Reproduction and regression tests for post-expansion delivery verifier wiring and prose-only financial binding."""

from unittest.mock import patch


class _PassingVerifier:
    def is_ready(self) -> bool:
        return True

    def verify(self, text: str = "", context: str = "", citations=None, **kwargs):
        from schemas.contracts.verification import VerificationRecord

        return VerificationRecord(
            verification_id="ver_1",
            workspace_id="ws_post_exp_deliv",
            query_id="q_1",
            snapshot_id="snap_1",
            requirement_id="req_1",
            evidence_id="exp_c1",
            verifier_version="1.0",
            verifier_type="mechanical",
            attempt_number=1,
            execution_time_ms=5,
            support_status="ASSERTED",
            confidence_score=0.95,
            verification_rationale="Supported by retained evidence.",
            verified_at="2026-11-27T00:00:00Z",
        )


def test_query_service_post_expansion_delivery_verifier_contract(monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.contracts.laya import LayaEscalationDecision, LexicalExpansionParams
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    ws = "ws_post_exp_deliv"
    _BM25_CACHE.clear()
    docs = [{"id": "doc_exp", "filename": "exp_notes.pdf"}]
    chs = [
        Chunk(
            id="exp_c1",
            document_id="doc_exp",
            element_type="NarrativeText",
            text="Commissioning completed under protocol Z94 in Q4 2026.",
            page_number=1,
            source_format="pdf",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        )
    ]

    class _Repo:
        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace(ws, "tester", workspace_id=ws)
    b_repo.create_source(ws, "exp_notes.pdf", source_id="doc_exp")
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws, document_id="doc_exp", source="exp_notes.pdf", version="v1",
        format="pdf", created_at="2026-11-27T00:00:00Z",
    ))
    repo_inst = _Repo()

    decision = LayaEscalationDecision(
        decision_id="dec_1",
        route="lexical_expansion",
        confidence=0.9,
        rationale="Add protocol Z94 term",
        parameters=LexicalExpansionParams(
            route_type="lexical_expansion",
            query_terms=["commissioning", "protocol", "Z94"],
        ),
    )

    from services.retrieval.fidelity_check import CoverageError

    def _make_fidelity_mock():
        calls = 0

        def _mock_cf(q, contexts, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise CoverageError("Missing post-expansion terms")
            return 1.0

        return _mock_cf

    # First verify QueryService fails closed when no semantic_verifier is configured
    qs_no_ver = QueryService(repo=repo_inst)
    with (
        patch("services.langgraph_pipeline.settings.LAYA_ENABLED", True),
        patch("services.langgraph_pipeline.LayaDecisionService.decide", return_value=decision),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_make_fidelity_mock()),
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "Commissioning completed under protocol Z94 in Q4 2026.",
            "citations": ["exp_c1"],
            "usage": {"input_tokens": 50, "output_tokens": 20, "stop_reason": "end_turn"},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_no_ver = qs_no_ver.execute_query(QueryRequest(
            query="What protocol governed commissioning?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "couldn't find" in out_no_ver["answer"].lower()
    assert out_no_ver["citations"] == []

    # Now verify QueryService wires semantic_verifier through when configured
    qs_with_ver = QueryService(repo=repo_inst, semantic_verifier=_PassingVerifier())
    with (
        patch("services.langgraph_pipeline.settings.LAYA_ENABLED", True),
        patch("services.langgraph_pipeline.LayaDecisionService.decide", return_value=decision),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_make_fidelity_mock()),
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "Commissioning completed under protocol Z94 in Q4 2026.",
            "citations": ["exp_c1"],
            "usage": {"input_tokens": 50, "output_tokens": 20, "stop_reason": "end_turn"},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_with_ver = qs_with_ver.execute_query(QueryRequest(
            query="What protocol governed commissioning?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "Z94" in out_with_ver["answer"]
    assert out_with_ver["final_citation_chunk_ids"] == ["exp_c1"]


def test_prose_only_financial_fixtures_and_negative_controls():
    from schemas.models import Chunk
    from services.langgraph_pipeline import delivery_gate
    from services.retrieval.table_metric_binding import bind_financial_query

    # Fixture 1: single-line prose financial chunk
    c1 = Chunk(
        id="prose_fin_1",
        document_id="doc_10q",
        element_type="NarrativeText",
        text="Acme Corp reported operating income of $42.5 million for the three months ended June 27, 2026.",
        page_number=3,
        source_format="pdf",
        workspace_id="ws_prose_fin",
    )
    q1 = "What was Acme Corp's operating income for the three months ended June 27, 2026?"
    res1 = delivery_gate({
        "query": q1,
        "workspace_id": "ws_prose_fin",
        "final_answer": "Acme Corp reported operating income of $42.5 million for the three months ended June 27, 2026.",
        "citations": [{"document_id": "doc_10q", "chunk_id": "prose_fin_1"}],
        "top_chunks": [c1],
        "financial_binding": bind_financial_query(q1),
    })
    assert res1 == {} or "42.5" in res1.get("final_answer", "")

    # Fixture 2: multi-line prose financial chunk
    c2 = Chunk(
        id="prose_fin_2",
        document_id="doc_10q",
        element_type="NarrativeText",
        text="Management Discussion\nIn the three months ended June 27, 2026, Acme Corp generated net income of $18.4 million.",
        page_number=4,
        source_format="pdf",
        workspace_id="ws_prose_fin",
    )
    q2 = "What was Acme Corp's net income for the three months ended June 27, 2026?"
    res2 = delivery_gate({
        "query": q2,
        "workspace_id": "ws_prose_fin",
        "final_answer": "Acme Corp generated net income of $18.4 million for the three months ended June 27, 2026.",
        "citations": [{"document_id": "doc_10q", "chunk_id": "prose_fin_2"}],
        "top_chunks": [c2],
        "financial_binding": bind_financial_query(q2),
    })
    assert res2 == {} or "18.4" in res2.get("final_answer", "")

    # Negative control 1: wrong metric
    q_wrong_metric = "What was Acme Corp's net income for the three months ended June 27, 2026?"
    res_wm = delivery_gate({
        "query": q_wrong_metric,
        "workspace_id": "ws_prose_fin",
        "final_answer": "Acme Corp reported operating income of $42.5 million for the three months ended June 27, 2026.",
        "citations": [{"document_id": "doc_10q", "chunk_id": "prose_fin_1"}],
        "top_chunks": [c1],
        "financial_binding": bind_financial_query(q_wrong_metric),
    })
    assert res_wm["final_answer"] == "NOT_FOUND"

    # Negative control 2: wrong period
    q_wrong_period = "What was Acme Corp's operating income for the three months ended June 28, 2025?"
    res_wp = delivery_gate({
        "query": q_wrong_period,
        "workspace_id": "ws_prose_fin",
        "final_answer": "Acme Corp reported operating income of $42.5 million for the three months ended June 27, 2026.",
        "citations": [{"document_id": "doc_10q", "chunk_id": "prose_fin_1"}],
        "top_chunks": [c1],
        "financial_binding": bind_financial_query(q_wrong_period),
    })
    assert res_wp["final_answer"] == "NOT_FOUND"

    # Negative control 3: unsupported value
    res_uv = delivery_gate({
        "query": q1,
        "workspace_id": "ws_prose_fin",
        "final_answer": "Acme Corp reported operating income of $99.9 million for the three months ended June 27, 2026.",
        "citations": [{"document_id": "doc_10q", "chunk_id": "prose_fin_1"}],
        "top_chunks": [c1],
        "financial_binding": bind_financial_query(q1),
    })
    assert res_uv["final_answer"] == "NOT_FOUND"


def test_deterministic_excerpt_financial_controls_and_zero_synthesis(monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE
    from services.retrieval.extractor import extract_verified_fact
    from services.retrieval.fidelity_check import CoverageError
    from services.retrieval.laya_service import LayaDecisionService

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    # 1. Correct fact through QueryService with semantic_verifier=None and zero synthesis calls
    ws = "ws_det_excerpt_ctrl"
    _BM25_CACHE.clear()
    docs = [{"id": "doc_rep", "filename": "annual_2027.pdf"}]
    chs = [
        Chunk(
            id="rep_2027",
            document_id="doc_rep",
            element_type="NarrativeText",
            text="Annual turnover for fiscal year 2027 was $610 million.",
            page_number=2,
            source_format="pdf",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        )
    ]

    class _Repo:
        def __init__(self):
            self.calls = 0

        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return [
                    Chunk(
                        id="dist_1",
                        document_id="doc_rep",
                        element_type="NarrativeText",
                        text="2027 compliance schedule.",
                        page_number=1,
                        source_format="pdf",
                        workspace_id=ws,
                        version="v1",
                        document_version="v1",
                    )
                ]
            _BM25_CACHE.clear()
            return chs
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace(ws, "tester", workspace_id=ws)
    b_repo.create_source(ws, "annual_2027.pdf", source_id="doc_rep")
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws, document_id="doc_rep", source="annual_2027.pdf", version="v1",
        format="pdf", created_at="2026-11-27T00:00:00Z",
    ))
    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)  # semantic_verifier=None

    orig_init = LayaDecisionService.__init__

    def _det_init(self, *a, **kw):
        orig_init(self, client=None, endpoint_url="", api_key="")
        self.client = None
        self.endpoint_url = ""

    cf_calls = 0

    def _cf(q, ctx, **kw):
        nonlocal cf_calls
        cf_calls += 1
        if cf_calls == 1:
            from services.retrieval.bm25_retriever import invalidate_chunk_cache
            invalidate_chunk_cache(ws)
            raise CoverageError("Missing turnover coverage")
        return 1.0

    def _forbid_llm(*a, **kw):
        raise AssertionError("call_llm must not be called on deterministic_excerpt path")

    with (
        patch("services.langgraph_pipeline.settings.LAYA_ENABLED", True),
        patch.object(LayaDecisionService, "__init__", _det_init),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_cf),
        patch("services.langgraph_pipeline.call_llm", side_effect=_forbid_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="What was the revenue for fiscal year 2027?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert out["executed_path"] == "deterministic_excerpt"
    assert "610 million" in out["answer"]
    assert out["final_citation_chunk_ids"] == ["rep_2027"]

    # 2. Wrong year control
    assert extract_verified_fact("What was the revenue for fiscal year 2025?", chs) == (None, None)

    # 3. Same number attached to a different metric control
    diff_metric_chunk = Chunk(
        id="rep_diff",
        document_id="doc_rep",
        element_type="NarrativeText",
        text="Total operating expenses for fiscal year 2027 were $610 million.",
        page_number=3,
        source_format="pdf",
        workspace_id=ws,
    )
    assert extract_verified_fact("What was the revenue for fiscal year 2027?", [diff_metric_chunk]) == (None, None)

    # 4. Conflicting values control
    conflict_chunk = Chunk(
        id="rep_conflict",
        document_id="doc_rep",
        element_type="NarrativeText",
        text="Annual turnover for fiscal year 2027 was $590 million.",
        page_number=4,
        source_format="pdf",
        workspace_id=ws,
    )
    assert extract_verified_fact("What was the revenue for fiscal year 2027?", chs + [conflict_chunk]) == (None, None)

    # 5. No evidence control
    assert extract_verified_fact("What was the R&D tax credit for fiscal year 2027?", chs) == (None, None)

    # 6. Tombstoned source control
    b_repo.record_source_tombstone(ws, "doc_rep", "usr_alice", "revoked")
    repo_inst.calls = 0
    _BM25_CACHE.clear()
    cf_calls = 0
    with (
        patch("services.langgraph_pipeline.settings.LAYA_ENABLED", True),
        patch.object(LayaDecisionService, "__init__", _det_init),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_cf),
        patch("services.langgraph_pipeline.call_llm", side_effect=_forbid_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_revoked = qs.execute_query(QueryRequest(
            query="What was the revenue for fiscal year 2027?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "couldn't find" in out_revoked["answer"].lower()
    assert out_revoked["citations"] == []


def test_standalone_deterministic_lexical_expansion_without_laya_enabled(monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE, invalidate_chunk_cache
    from services.retrieval.fidelity_check import CoverageError

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    ws = "ws_standalone_det_exp"
    _BM25_CACHE.clear()
    docs = [{"id": "doc_std", "filename": "orion_2028.pdf"}]
    dist_chunk = Chunk(
        id="std_dist_1", document_id="doc_std", element_type="NarrativeText",
        text="2028 compliance schedule.", page_number=1, source_format="pdf",
        workspace_id=ws, version="v1", document_version="v1",
    )
    target_chunk = Chunk(
        id="std_target_1", document_id="doc_std", element_type="NarrativeText",
        text="OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        page_number=2, source_format="pdf", workspace_id=ws, version="v1", document_version="v1",
    )

    class _Repo:
        def __init__(self):
            self.calls = 0

        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw):
            self.calls += 1
            if self.calls == 1:
                return [dist_chunk]
            _BM25_CACHE.clear()
            return [target_chunk]
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace(ws, "tester", workspace_id=ws)
    b_repo.create_source(ws, "orion_2028.pdf", source_id="doc_std")
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws, document_id="doc_std", source="orion_2028.pdf", version="v1",
        format="pdf", created_at="2026-11-27T00:00:00Z",
    ))
    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)

    cf_calls = 0

    def _cf(q, ctx, **kw):
        nonlocal cf_calls
        cf_calls += 1
        if cf_calls == 1:
            invalidate_chunk_cache(ws)
            raise CoverageError("Missing turnover coverage")
        return 1.0

    def _forbid_laya(*a, **kw):
        raise AssertionError("LayaDecisionService must not be initialized in standalone deterministic mode")

    def _forbid_llm(*a, **kw):
        raise AssertionError("call_llm must not be called on deterministic_excerpt path")

    with (
        patch("services.langgraph_pipeline.settings.LAYA_ENABLED", False),
        patch("services.langgraph_pipeline.settings.DETERMINISTIC_LEXICAL_EXPANSION_ENABLED", True),
        patch("services.langgraph_pipeline.LayaDecisionService.__init__", side_effect=_forbid_laya),
        patch("services.langgraph_pipeline.check_fidelity", side_effect=_cf),
        patch("services.langgraph_pipeline.call_llm", side_effect=_forbid_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="What was OrionBio's revenue for fiscal year 2028?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert out["executed_path"] == "deterministic_excerpt"
    assert "740 million" in out["answer"]
    assert out["final_citation_chunk_ids"] == ["std_target_1"]
    rc = out["retrieval_candidates"]
    assert rc["laya_dispatched"] is False
    assert rc["laya_provider"] == "deterministic"


def test_bare_uuid_hint_reproduction_and_negative_controls(monkeypatch):
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    ws = "ws_bare_uuid_hints"
    docs = [
        {"id": "8fd04e2d-bd45-51a4-8b34-1473525debdf", "filename": "orionbio_summary.docx"},
        {"id": "5bc9e5c9-0650-5cd2-8284-a17fe144ab1f", "filename": "novachem_q1.docx"},
    ]
    c_orion = Chunk(
        id="8fd04e2d-bd45-51a4-8b34-1473525debdf:p:0",
        document_id="8fd04e2d-bd45-51a4-8b34-1473525debdf",
        element_type="NarrativeText",
        text="OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        page_number=1, source_format="docx", workspace_id=ws, version="v1", document_version="v1",
    )
    c_orion_hr = Chunk(
        id="8fd04e2d-bd45-51a4-8b34-1473525debdf:p:1",
        document_id="8fd04e2d-bd45-51a4-8b34-1473525debdf",
        element_type="NarrativeText",
        text="OrionBio's employee turnover for fiscal year 2026 was 11%.",
        page_number=1, source_format="docx", workspace_id=ws, version="v1", document_version="v1",
    )
    c_nova = Chunk(
        id="5bc9e5c9-0650-5cd2-8284-a17fe144ab1f:p:0",
        document_id="5bc9e5c9-0650-5cd2-8284-a17fe144ab1f",
        element_type="NarrativeText",
        text="NovaChem's Q1 2028 operating margin was 19.5% across specialty polymers.",
        page_number=1, source_format="docx", workspace_id=ws, version="v1", document_version="v1",
    )

    case_counter = 0

    def _run_case(chunks, query_text, stub_answer, stub_citations, snap_ver="v1"):
        nonlocal case_counter
        case_counter += 1
        case_ws = f"{ws}_{case_counter}"
        _BM25_CACHE.clear()
        llm_calls = 0

        from dataclasses import replace as _dc_replace
        ws_chunks = [_dc_replace(c, workspace_id=case_ws) for c in chunks]

        class _Repo:
            def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
            def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return ws_chunks
            def get(self, doc_id): return None
            def search_vector(self, *a, **kw): return []
            def check_semantic_cache(self, *a, **kw): return None

        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace(case_ws, "tester", workspace_id=case_ws)
        for d in docs:
            b_repo.create_source(case_ws, d["filename"], source_id=d["id"])
            b_repo.save_document_snapshot(DocumentSnapshot(
                workspace_id=case_ws, document_id=d["id"], source=d["filename"], version=snap_ver,
                format="docx", created_at="2026-11-27T00:00:00Z",
            ))

        def _stub_llm(*a, **kw):
            nonlocal llm_calls
            llm_calls += 1
            return {
                "answer": stub_answer,
                "citations": stub_citations,
                "usage": {"input_tokens": 30, "output_tokens": 12, "stop_reason": "end_turn"},
            }

        repo_inst = _Repo()
        qs = QueryService(repo=repo_inst)
        with (
            patch("services.langgraph_pipeline.call_llm", side_effect=_stub_llm),
            patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            out = qs.execute_query(QueryRequest(query=query_text, workspace_id=case_ws, cache=False, benchmark_mode=True))
        assert llm_calls <= 1
        return out

    # 1. T01 & T02 bare UUID resolution
    out_t01 = _run_case(
        [c_orion, c_orion_hr],
        "What was OrionBio's revenue for fiscal year 2028?",
        "OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        ["8fd04e2d-bd45-51a4-8b34-1473525debdf"],
    )
    assert "740 million" in out_t01["answer"]
    assert out_t01["final_citation_chunk_ids"] == ["8fd04e2d-bd45-51a4-8b34-1473525debdf:p:0"]

    out_t02 = _run_case(
        [c_nova],
        "What was NovaChem's Q1 2028 operating margin?",
        "NovaChem's Q1 2028 operating margin was 19.5%.",
        ["5bc9e5c9-0650-5cd2-8284-a17fe144ab1f"],
    )
    assert "19.5%" in out_t02["answer"]
    assert out_t02["final_citation_chunk_ids"] == ["5bc9e5c9-0650-5cd2-8284-a17fe144ab1f:p:0"]

    # 2. Wrong-document control (cites NovaChem UUID for OrionBio query)
    out_wrong_doc = _run_case(
        [c_orion, c_nova],
        "What was OrionBio's revenue for fiscal year 2028?",
        "OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        ["5bc9e5c9-0650-5cd2-8284-a17fe144ab1f"],
    )
    assert "couldn't find" in out_wrong_doc["answer"].lower()
    assert out_wrong_doc["citations"] == []

    # 3. Stale-version control (chunk v1 vs snapshot v2)
    out_stale = _run_case(
        [c_orion],
        "What was OrionBio's revenue for fiscal year 2028?",
        "OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        ["8fd04e2d-bd45-51a4-8b34-1473525debdf"],
        snap_ver="v2",
    )
    assert "couldn't find" in out_stale["answer"].lower()
    assert out_stale["citations"] == []

    # 4. Multiple-candidate control (2 matching chunks in same doc -> ambiguous -> refuse)
    c_orion_dup = Chunk(
        id="8fd04e2d-bd45-51a4-8b34-1473525debdf:p:2",
        document_id="8fd04e2d-bd45-51a4-8b34-1473525debdf",
        element_type="NarrativeText",
        text="OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        page_number=2, source_format="docx", workspace_id=ws, version="v1", document_version="v1",
    )
    out_multi = _run_case(
        [c_orion, c_orion_dup],
        "What was OrionBio's revenue for fiscal year 2028?",
        "OrionBio's annual turnover for fiscal year 2028 was $740 million.",
        ["8fd04e2d-bd45-51a4-8b34-1473525debdf"],
    )
    assert "couldn't find" in out_multi["answer"].lower()
    assert out_multi["citations"] == []

    # 5. Unsupported single-chunk control ($999 million vs $740 million)
    out_unsup = _run_case(
        [c_orion],
        "What was OrionBio's revenue for fiscal year 2028?",
        "OrionBio's annual turnover for fiscal year 2028 was $999 million.",
        ["8fd04e2d-bd45-51a4-8b34-1473525debdf"],
    )
    assert "couldn't find" in out_unsup["answer"].lower()
    assert out_unsup["citations"] == []


def test_real_fidelity_rejects_distractors_missing_entity_metric_or_period():
    import pytest
    from services.retrieval.fidelity_check import CoverageError, check_fidelity

    q = "What was MeridianLogistics's revenue for fiscal year 2030?"
    with pytest.raises(CoverageError):
        check_fidelity(q, ["NorthwindFreight 2030 annual turnover was $310 million."])
    with pytest.raises(CoverageError):
        check_fidelity(q, ["MeridianLogistics 2029 annual turnover was $310 million."])
    with pytest.raises(CoverageError):
        check_fidelity(q, ["MeridianLogistics 2030 2030 2030 fleet maintenance schedule section 4."])
    with pytest.raises(CoverageError):
        check_fidelity(q, ["MeridianLogistics employee turnover for fiscal year 2030 was 9%."])

    score = check_fidelity(q, ["MeridianLogistics's annual turnover for fiscal year 2030 was $520 million."])
    assert score > 0.0


def test_controlled_http_standalone_deterministic_expansion(monkeypatch):
    """Controlled HTTP integration test: first-pass retrieval returns distractors; standalone expansion recovers."""
    from fastapi.testclient import TestClient

    import main as app_main
    from config import settings
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import query_service
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE, invalidate_chunk_cache

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")
    monkeypatch.setenv("GRAPH_ENABLED", "false")

    ws = "ws_ctrl_http_exp"
    doc_id = "doc_ctrl_helios"
    _BM25_CACHE.clear()
    invalidate_chunk_cache(ws)

    dist_chunks = [
        Chunk(
            id=f"ctrl_dist_{i}",
            document_id=doc_id,
            element_type="NarrativeText",
            text=f"HeliosGroup 2029 2029 2029 schedule section {i}.",
            page_number=i,
            source_format="docx",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        )
        for i in range(1, 5)
    ]
    target_chunk = Chunk(
        id="ctrl_target_2029",
        document_id=doc_id,
        element_type="NarrativeText",
        text="HeliosGroup's annual turnover for fiscal year 2029 was $880 million.",
        page_number=5,
        source_format="docx",
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    class _CtrlRepo:
        def get_workspace_documents(self, workspace_id, page=1, limit=1000):
            return {"documents": [{"id": doc_id, "filename": "helios_2029.docx"}]}

        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw):
            all_c = dist_chunks + [target_chunk]
            if document_ids:
                s = set(document_ids)
                return [c for c in all_c if c.document_id in s]
            return all_c

        def get(self, d_id):
            return None

        def search_vector(self, *a, **kw):
            return []

        def check_semantic_cache(self, *a, **kw):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace(ws, "usr_alice", workspace_id=ws)
    b_repo.create_source(ws, "helios_2029.docx", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws,
            document_id=doc_id,
            source="helios_2029.docx",
            version="v1",
            format="docx",
            created_at="2026-11-27T00:00:00Z",
        )
    )

    # Control ONLY the first-pass retrieval result (BM25Retriever.search & VectorRetriever.search)
    import db.workspace_boundary_repo as _wbr
    import src.db.workspace_boundary_repo as _swbr
    from services.retrieval.bm25_retriever import BM25Retriever
    from services.retrieval.vector_retriever import VectorRetriever

    _orig_bm25_search = BM25Retriever.search

    def _controlled_bm25_search(self, query, chunks, top_k=10, **kw):
        if query.startswith("What was"):
            return [(c, 1.0) for c in dist_chunks[:3]]
        return _orig_bm25_search(self, query, chunks, top_k=top_k, **kw)

    repo_inst = _CtrlRepo()
    monkeypatch.setattr(BM25Retriever, "search", _controlled_bm25_search)
    monkeypatch.setattr(VectorRetriever, "search", lambda self, *a, **kw: [])
    monkeypatch.setattr("services.langgraph_pipeline._get_repo", lambda: repo_inst)
    monkeypatch.setattr(_wbr, "get_workspace_boundary_repo", lambda: b_repo)
    monkeypatch.setattr(_swbr, "get_workspace_boundary_repo", lambda: b_repo)
    monkeypatch.setattr(query_service, "repo", repo_inst)

    orig_laya = settings.LAYA_ENABLED
    orig_det = settings.DETERMINISTIC_LEXICAL_EXPANSION_ENABLED
    try:
        client = TestClient(app_main.app)
        headers = {"Authorization": "Bearer test_token_alice"}
        payload = {"query": "What was HeliosGroup's revenue for fiscal year 2029?", "workspace_id": ws, "cache": False}

        # Arm A: OFF -> refuses
        settings.LAYA_ENABLED = False
        settings.DETERMINISTIC_LEXICAL_EXPANSION_ENABLED = False
        r_off = client.post("/api/v1/query", json=payload, headers=headers)
        assert r_off.status_code == 200
        b_off = r_off.json()
        assert "couldn't find" in b_off["answer"].lower()
        assert b_off["citations"] == []
        assert b_off["generation_calls"] == 0

        # Arm B: ON -> recovers via deterministic expansion with 0 routing-provider & 0 synthesis calls
        settings.DETERMINISTIC_LEXICAL_EXPANSION_ENABLED = True
        r_on = client.post("/api/v1/query", json=payload, headers=headers)
        assert r_on.status_code == 200
        b_on = r_on.json()
        assert "880 million" in b_on["answer"]
        assert b_on["executed_path"] == "deterministic_excerpt"
        assert b_on["generation_calls"] == 0
        assert [c["chunk_id"] for c in b_on["citations"]] == ["ctrl_target_2029"]
        rc = b_on["retrieval_candidates"]
        assert rc["laya_dispatched"] is False
        assert rc["laya_provider"] == "deterministic"
        assert rc["laya_expansion_executed"] is True
    finally:
        settings.LAYA_ENABLED = orig_laya
        settings.DETERMINISTIC_LEXICAL_EXPANSION_ENABLED = orig_det


def test_merged_year_h01_prose_verification_and_controls(monkeypatch):
    """Merged 2025+2026 chunk: both years pass, swapped values and same-year conflicts refuse."""
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE, invalidate_chunk_cache

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    doc_id = "doc_fin_merged"
    merged_text = (
        "Fiscal Year 2025 Summary: Consolidated turnover was $380 million with 12% operating margin. "
        "The annual turnover for fiscal year 2026 was $450 million."
    )

    def _run_merged(case_name: str, query: str, model_ans: str, extra_chunks: list[Chunk] | None = None):
        ws = f"ws_merged_{case_name}"
        _BM25_CACHE.clear()
        invalidate_chunk_cache(ws)
        c_main = Chunk(
            id=f"{doc_id}:p:0",
            document_id=doc_id,
            element_type="NarrativeText",
            text=merged_text,
            page_number=1,
            source_format="docx",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        )
        chunks = [c_main] + (extra_chunks or [])

        class _Repo:
            def get_workspace_documents(self, workspace_id, page=1, limit=1000):
                return {"documents": [{"id": doc_id, "filename": "annual_financials_2026.docx"}]}

            def get_all_chunks(self, document_ids=None, workspace_id=None, **kw):
                return chunks

            def get(self, d_id):
                return None

            def search_vector(self, *a, **kw):
                return []

            def check_semantic_cache(self, *a, **kw):
                return None

        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace(ws, "tester", workspace_id=ws)
        b_repo.create_source(ws, "annual_financials_2026.docx", source_id=doc_id)
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws,
                document_id=doc_id,
                source="annual_financials_2026.docx",
                version="v1",
                format="docx",
                created_at="2026-11-27T00:00:00Z",
            )
        )
        repo_inst = _Repo()
        svc = QueryService(repo=repo_inst)
        with patch("services.langgraph_pipeline.call_llm", return_value={"answer": model_ans, "citations": [c_main.id], "usage": {"input_tokens": 30, "output_tokens": 12, "stop_reason": "end_turn"}}), \
             patch("services.langgraph_pipeline._get_repo", return_value=repo_inst), \
             patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo), \
             patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo), \
             patch("services.langgraph_pipeline.check_fidelity", return_value=1.0):
            return svc.execute_query(QueryRequest(query=query, workspace_id=ws, cache=False, benchmark_mode=True))

    # 1. Correct 2026 answer passes
    r_2026 = _run_merged("ok26", "What was the revenue for fiscal year 2026?", "The revenue for fiscal year 2026 was $450 million.")
    assert "450 million" in r_2026["answer"]
    assert r_2026["citations"] and r_2026["citations"][0]["chunk_id"] == f"{doc_id}:p:0"

    # 2. Correct 2025 answer passes
    r_2025 = _run_merged("ok25", "What was the revenue for fiscal year 2025?", "Consolidated turnover was $380 million in fiscal year 2025.")
    assert "380 million" in r_2025["answer"]
    assert r_2025["citations"] and r_2025["citations"][0]["chunk_id"] == f"{doc_id}:p:0"

    # 3. Swapped 2026 answer ($380M from 2025) refuses
    r_swap26 = _run_merged("swap26", "What was the revenue for fiscal year 2026?", "The revenue for fiscal year 2026 was $380 million.")
    assert "couldn't find" in r_swap26["answer"].lower()
    assert r_swap26["citations"] == []

    # 4. Swapped 2025 answer ($450M from 2026) refuses
    r_swap25 = _run_merged("swap25", "What was the revenue for fiscal year 2025?", "The revenue for fiscal year 2025 was $450 million.")
    assert "couldn't find" in r_swap25["answer"].lower()
    assert r_swap25["citations"] == []

    # 5. Conflicting values for 2026 ($450M vs $490M) refuses
    c_conflict = Chunk(
        id=f"{doc_id}:p:1",
        document_id=doc_id,
        element_type="NarrativeText",
        text="Restated annual turnover for fiscal year 2026 was $490 million.",
        page_number=2,
        source_format="docx",
        workspace_id="ws_merged_conf26",
        version="v1",
        document_version="v1",
    )
    r_conf = _run_merged("conf26", "What was the revenue for fiscal year 2026?", "The revenue for fiscal year 2026 was $450 million.", extra_chunks=[c_conflict])
    assert "couldn't find" in r_conf["answer"].lower()
    assert r_conf["citations"] == []
