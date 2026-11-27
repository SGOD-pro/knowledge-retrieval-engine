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
