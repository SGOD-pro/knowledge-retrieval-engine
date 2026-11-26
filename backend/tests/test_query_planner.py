import pytest

from services.retrieval.deterministic_executor import ExecutionOperator
from services.retrieval.llm_budget import LLMCallBudget, LLMBudgetExceeded
from services.retrieval.subgoal_decomposer import (
    QueryIntent,
    decompose_query,
    detect_output_contract,
    parse_execution_operator,
)


def test_detect_output_contracts():
    assert detect_output_contract("Return the summary as JSON") == "json"
    assert detect_output_contract("Is it true that revenue increased?") == "boolean"
    assert detect_output_contract("List all colleges in Karnataka") == "list"
    assert detect_output_contract("How many districts were surveyed?") == "number"
    assert detect_output_contract("Explain the mechanism of attention") == "text"


def test_parse_operators():
    assert parse_execution_operator("Calculate percentage difference between A and B") == ExecutionOperator.PERCENTAGE_DIFFERENCE
    assert parse_execution_operator("What is the difference between 2021 and 2022?") == ExecutionOperator.DIFFERENCE
    assert parse_execution_operator("Find the ratio of debt to equity") == ExecutionOperator.RATIO
    assert parse_execution_operator("What was the average score?") == ExecutionOperator.AVERAGE
    assert parse_execution_operator("How many households were surveyed?") == ExecutionOperator.COUNT


def test_structural_target_detection():
    plan = decompose_query("In Table 4, what was the total capital expenditure?")
    assert plan.requires_structural_retrieval is True
    assert plan.subgoals[0].structural_hint == "table 4"


def test_multi_hop_query_decomposition():
    query = "Compare revenue from SEC Filing with turnover from Annual Report"
    plan = decompose_query(query)
    assert plan.primary_intent == QueryIntent.MULTI_HOP
    assert len(plan.subgoals) == 2
    assert "revenue" in plan.subgoals[0].query_text.lower()
    assert "turnover" in plan.subgoals[1].query_text.lower()
    assert plan.root_operator == ExecutionOperator.COMPARE


def test_mechanical_llm_budget_enforcer():
    budget = LLMCallBudget(max_calls=1)
    # First call must succeed
    budget.acquire()
    assert budget.calls_made == 1

    # Second call must raise LLMBudgetExceeded
    with pytest.raises(LLMBudgetExceeded) as exc:
        budget.acquire()
    assert "maximum budget is 1" in str(exc.value)

    # Reset allows reuse in tests
    budget.reset()
    assert budget.calls_made == 0
    budget.acquire()


def test_multi_source_requirement_ids_survive_and_missing_source_refuses(monkeypatch):
    """Multi-source requirements survive 6-slot selection and missing explicit sources refuse safely."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    query = "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 2026 current ratio with the 2025 Retail H36 value."
    plan = decompose_query(query)
    assert len(plan.subgoals) == 2
    assert plan.subgoals[0].eligible_sources == ("acme_10q.pdf", "industry_metrics.csv")
    assert plan.subgoals[1].eligible_sources == ("acme_10q.pdf", "industry_metrics.csv")

    def _make_chunks(ws: str):
        pdf_chunks = [
            Chunk(id="pdf_bal", document_id="doc_pdf", element_type="Table", text="Acme Corp Balance Sheet June 30, 2026. Current assets 200,000. Current liabilities 100,000. June 2026 current ratio.", page_number=5, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda1", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp MD&A June 2026 current ratio analysis compared with 2025 Retail benchmark value.", page_number=12, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda2", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp June 2026 current ratio liquidity discussion against 2025 Retail metrics.", page_number=13, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda3", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp 2026 financial condition, current ratio, and Retail benchmark comparison for 2025.", page_number=14, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda4", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 4: June 2026 liquidity and current ratio disclosures, comparing 2026 with 2025.", page_number=15, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda5", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 5: June 2026 Retail segment current ratio and 2025 benchmark value.", page_number=16, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_distractor", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 9: Retail store count was 84 in 2025. Retail H36 lease classification benchmark value 84 applies to store leases, not current ratio.", page_number=18, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
        ]
        csv_chunks = [
            Chunk(id="csv_h01", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Variable_code: H01. Variable_name: Total sales. Value: 50000.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="csv_h36", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: 84.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1"),
        ]
        return pdf_chunks, csv_chunks

    class _StubRepo:
        def __init__(self, chunks, docs):
            self._chunks = chunks
            self._docs = docs
        def get_workspace_documents(self, workspace_id, page=1, limit=1000):
            return {"documents": self._docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw):
            return self._chunks
        def get(self, doc_id):
            return None
        def search_vector(self, *a, **kw):
            return []
        def check_semantic_cache(self, *a, **kw):
            return None

    llm_calls = []
    def _fake_llm(q, ctx, **kw):
        llm_calls.append(ctx)
        return {
            "answer": "Acme's current ratio is 200% vs Retail H36 value 84%.",
            "citations": ["pdf_mda4", "csv_h36"] if "csv_h36" in ctx else ["pdf_distractor"],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }

    # 1. Both sources present: both requirement IDs and evidence survive within 6 slots
    ws_both = "ws_planner_both_sources"
    p_chs, c_chs = _make_chunks(ws_both)
    docs_both = [{"id": "doc_pdf", "filename": "acme_10q.pdf"}, {"id": "doc_csv", "filename": "industry_metrics.csv"}]
    repo_both = _StubRepo(p_chs + c_chs, docs_both)
    b_both = InMemoryWorkspaceBoundaryRepository()
    b_both.create_workspace("Both", "tester", workspace_id=ws_both)
    for d in docs_both:
        b_both.create_source(ws_both, d["filename"], source_id=d["id"])
        b_both.save_document_snapshot(DocumentSnapshot(workspace_id=ws_both, document_id=d["id"], source=d["filename"], version="v1", format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z"))

    qs_both = QueryService(repo=repo_both)
    with (
        patch("services.langgraph_pipeline.call_llm", side_effect=_fake_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_both),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_both),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_both),
    ):
        out_both = qs_both.execute_query(QueryRequest(query=query, workspace_id=ws_both, cache=False, benchmark_mode=True))

    assert len(out_both["compressed_context_chunk_ids"]) <= 6
    assert "csv_h36" in out_both["compressed_context_chunk_ids"]
    assert out_both["requirement_ids"] == ["sub_1", "sub_2"]
    assert out_both["requirement_evidence"]["sub_2"] == ["csv_h36"]
    assert out_both["missing_required_sources"] == []
    assert len(llm_calls) == 0

    # 2. Second source missing + distractor in PDF: refuses without calling LLM or citing distractor
    llm_calls.clear()
    ws_miss = "ws_planner_missing_second"
    p_only, _ = _make_chunks(ws_miss)
    docs_miss = [{"id": "doc_pdf", "filename": "acme_10q.pdf"}]
    repo_miss = _StubRepo(p_only, docs_miss)
    b_miss = InMemoryWorkspaceBoundaryRepository()
    b_miss.create_workspace("Missing", "tester", workspace_id=ws_miss)
    b_miss.create_source(ws_miss, "acme_10q.pdf", source_id="doc_pdf")
    b_miss.save_document_snapshot(DocumentSnapshot(workspace_id=ws_miss, document_id="doc_pdf", source="acme_10q.pdf", version="v1", format="pdf", created_at="2026-11-26T00:00:00Z"))

    qs_miss = QueryService(repo=repo_miss)
    with (
        patch("services.langgraph_pipeline.call_llm", side_effect=_fake_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_miss),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_miss),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_miss),
    ):
        out_miss = qs_miss.execute_query(QueryRequest(query=query, workspace_id=ws_miss, cache=False, benchmark_mode=True))

    assert out_miss["missing_required_sources"] == ["industry_metrics.csv"]
    assert out_miss["citations"] == []
    assert len(llm_calls) == 0
    assert "couldn't find" in out_miss["answer"].lower()


def test_decompose_query_prefix_source_hints_and_non_filename_no_false_refusal(monkeypatch):
    """Prefix filenames populate source_hint only when two exist, and non-filename queries never trigger missing_required_sources."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk

    plan_xlsx = decompose_query("From report_q2.pdf and metrics_2025.xlsx, compare revenue with target.")
    assert len(plan_xlsx.subgoals) == 2
    assert plan_xlsx.subgoals[0].eligible_sources == ("report_q2.pdf", "metrics_2025.xlsx")
    assert plan_xlsx.subgoals[1].eligible_sources == ("report_q2.pdf", "metrics_2025.xlsx")

    plan_single_prefix = decompose_query("From report_q2.pdf, compare revenue with prior year.")
    assert len(plan_single_prefix.subgoals) == 2
    assert plan_single_prefix.subgoals[0].source_hint is None
    assert plan_single_prefix.subgoals[1].source_hint is None

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    ws = "ws_planner_non_filename"
    chs = [
        Chunk(id="bal_1", document_id="doc_1", element_type="Table", text="Uploaded balance sheet current assets 150 and current liabilities 100.", page_number=1, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
        Chunk(id="csv_1", document_id="doc_2", element_type="TableRow", text="Survey CSV 2025 H36 current ratio value 79.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1"),
    ]
    docs = [{"id": "doc_1", "filename": "10q.pdf"}, {"id": "doc_2", "filename": "survey_data.csv"}]

    class _Repo:
        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("NonFilename", "tester", workspace_id=ws)
    for d in docs:
        b_repo.create_source(ws, d["filename"], source_id=d["id"])
        b_repo.save_document_snapshot(DocumentSnapshot(workspace_id=ws, document_id=d["id"], source=d["filename"], version="v1", format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z"))

    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={"answer": "150% vs 79%.", "citations": ["bal_1", "csv_1"], "usage": {}}),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="Compare the uploaded balance sheet current ratio with the survey CSV H36 value.",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert out["missing_required_sources"] == []
    assert out["requirement_ids"] == ["sub_1", "sub_2"]
    assert len(out["citations"]) == 2


def test_multi_source_operand_binding_reverse_order_and_distractor_rejection(monkeypatch):
    """Multi-source comparisons bind real operands over narrative, handle reversed prefix filenames, and block distractor citations."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    def _make_chunks(ws: str, *, include_h36: bool = True):
        pdf_chunks = [
            Chunk(id="pdf_bal", document_id="doc_pdf", element_type="Table", text="Acme Corp Condensed Consolidated Balance Sheet June 30, 2026. Total current assets $200,000. Total current liabilities $100,000.", page_number=5, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda1", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp MD&A June 2026 current ratio analysis compared with 2025 Retail benchmark value.", page_number=12, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda2", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp June 2026 current ratio liquidity discussion against 2025 Retail metrics.", page_number=13, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda3", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp 2026 financial condition, current ratio, and Retail benchmark comparison for 2025.", page_number=14, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda4", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 4: June 2026 liquidity and current ratio disclosures, comparing 2026 with 2025.", page_number=15, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_mda5", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 5: June 2026 Retail segment current ratio and 2025 benchmark value.", page_number=16, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="pdf_distractor", document_id="doc_pdf", element_type="NarrativeText", text="Acme Corp Note 9: Retail store count was 84 in 2024. Retail H36 lease classification benchmark value 84 applies to store leases, not current ratio.", page_number=18, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
        ]
        csv_chunks = [
            Chunk(id="csv_h01", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Variable_code: H01. Variable_name: Total sales. Value: 50000.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1"),
        ]
        if include_h36:
            csv_chunks.append(
                Chunk(id="csv_h36", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: 84.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1")
            )
        return pdf_chunks, csv_chunks

    class _StubRepo:
        def __init__(self, chunks, docs):
            self._chunks = chunks
            self._docs = docs
        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": self._docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return self._chunks
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    def _run(ws: str, query: str, include_h36: bool, fake_ans: str, fake_cits: list[str]):
        p_chs, c_chs = _make_chunks(ws, include_h36=include_h36)
        docs = [{"id": "doc_pdf", "filename": "acme_10q.pdf"}, {"id": "doc_csv", "filename": "industry_metrics.csv"}]
        repo = _StubRepo(p_chs + c_chs, docs)
        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace(ws, "tester", workspace_id=ws)
        for d in docs:
            b_repo.create_source(ws, d["filename"], source_id=d["id"])
            b_repo.save_document_snapshot(DocumentSnapshot(workspace_id=ws, document_id=d["id"], source=d["filename"], version="v1", format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z"))
        qs = QueryService(repo=repo)
        with (
            patch("services.langgraph_pipeline.call_llm", return_value={"answer": fake_ans, "citations": fake_cits, "usage": {"input_tokens": 10, "output_tokens": 10}}),
            patch("services.langgraph_pipeline._get_repo", return_value=repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(QueryRequest(query=query, workspace_id=ws, cache=False, benchmark_mode=True))

    # 1. Valid comparison: real balance-sheet operand (pdf_bal) and CSV H36 (csv_h36) both survive into compressed context and final citations
    out_valid = _run(
        "ws_op_valid",
        "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value.",
        True,
        "Acme: 200,000 / 100,000 = 200%. Retail H36: 84%.",
        ["pdf_bal", "csv_h36"],
    )
    assert len(out_valid["compressed_context_chunk_ids"]) <= 6
    assert "pdf_bal" in out_valid["compressed_context_chunk_ids"]
    assert "csv_h36" in out_valid["compressed_context_chunk_ids"]
    assert set(out_valid["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}
    assert "200%" in out_valid["answer"] and "84%" in out_valid["answer"]

    # 2. Reversed prefix filenames still bind sub_1 to pdf_bal and sub_2 to csv_h36
    out_rev = _run(
        "ws_op_reverse",
        "Using industry_metrics.csv and acme_10q.pdf, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value.",
        True,
        "Acme: 200,000 / 100,000 = 200%. Retail H36: 84%.",
        ["pdf_bal", "csv_h36"],
    )
    assert "pdf_bal" in out_rev["requirement_evidence"]["sub_1"]
    assert "csv_h36" in out_rev["requirement_evidence"]["sub_2"]
    assert set(out_rev["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}

    # 3. Both files present, H36 missing from CSV, model cites distractor (wrong year 2024 / store count 84): must NOT deliver comparison
    out_distractor = _run(
        "ws_op_distractor",
        "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value.",
        False,
        "Acme: 200,000 / 100,000 = 200%. Retail H36: 84%.",
        ["pdf_bal", "pdf_distractor"],
    )
    assert out_distractor["citations"] == []
    assert "couldn't find" in out_distractor["answer"].lower()

    # 4. Fallback path (empty explicit model citations): valid comparison delivers both operands, distractor-only fails
    out_fallback_valid = _run(
        "ws_op_fb_valid",
        "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value.",
        True,
        "Acme: 200,000 / 100,000 = 200%. Retail H36: 84%.",
        [],
    )
    assert "200%" in out_fallback_valid["answer"] and "84%" in out_fallback_valid["answer"]
    assert set(out_fallback_valid["final_citation_chunk_ids"]) >= {"pdf_bal", "csv_h36"}

    out_fallback_dist = _run(
        "ws_op_fb_dist",
        "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value.",
        False,
        "Acme: 200,000 / 100,000 = 200%. Retail H36: 84%.",
        [],
    )
    assert out_fallback_dist["citations"] == []
    assert "couldn't find" in out_fallback_dist["answer"].lower()


def test_derived_comparison_direction_difference_and_scale_verification(monkeypatch):
    """Two-operand comparisons verify direction, derived differences, equal values, reversed operand order, and percentage-point vs relative-% scale."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    class _StubRepo:
        def __init__(self, chunks, docs):
            self._chunks = chunks
            self._docs = docs
        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": self._docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return self._chunks
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    def _run_case(ws: str, query: str, h36_val: str, fake_ans: str, fake_cits: list[str]):
        from services.retrieval.bm25_retriever import _BM25_CACHE
        _BM25_CACHE.clear()
        chs = [
            Chunk(id="pdf_bal", document_id="doc_pdf", element_type="Table", text="Acme Corp Condensed Consolidated Balance Sheet June 30, 2026. Total current assets $200,000. Total current liabilities $100,000.", page_number=5, source_format="pdf", workspace_id=ws, version="v1", document_version="v1"),
            Chunk(id="csv_h36", document_id="doc_csv", element_type="TableRow", text=f"Year: 2025. Industry: Retail. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: {h36_val}.", page_number=1, source_format="csv", workspace_id=ws, version="v1", document_version="v1"),
        ]
        docs = [{"id": "doc_pdf", "filename": "acme_10q.pdf"}, {"id": "doc_csv", "filename": "industry_metrics.csv"}]
        repo = _StubRepo(chs, docs)
        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace(ws, "tester", workspace_id=ws)
        for d in docs:
            b_repo.create_source(ws, d["filename"], source_id=d["id"])
            b_repo.save_document_snapshot(DocumentSnapshot(workspace_id=ws, document_id=d["id"], source=d["filename"], version="v1", format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z"))
        qs = QueryService(repo=repo)
        with (
            patch("services.langgraph_pipeline.call_llm", return_value={"answer": fake_ans, "citations": fake_cits, "usage": {"input_tokens": 10, "output_tokens": 10}}),
            patch("services.langgraph_pipeline._get_repo", return_value=repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(QueryRequest(query=query, workspace_id=ws, cache=False, benchmark_mode=True))

    q_fwd = "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value."
    q_rev = "Using acme_10q.pdf and industry_metrics.csv, compare the 2025 Retail H36 value with Acme's June 30, 2026 current ratio."
    q_rel = "Using acme_10q.pdf and industry_metrics.csv, compare Acme's June 30, 2026 current ratio with the 2025 Retail H36 value by percentage difference."

    # A. Direct QueryService deterministic rendering (zero LLM calls)
    out_pp_ok = _run_case("ws_cmp_ppok", q_fwd, "84", "IGNORED", [])
    assert "116 percentage points" in out_pp_ok["answer"] and "higher" in out_pp_ok["answer"].lower()
    assert set(out_pp_ok["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}
    assert out_pp_ok["generation_calls"] == 0

    out_eq_ok = _run_case("ws_cmp_eqok", q_fwd, "200", "IGNORED", [])
    assert "equals" in out_eq_ok["answer"].lower() and "0 percentage points" in out_eq_ok["answer"].lower()
    assert set(out_eq_ok["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}

    out_ord_ok = _run_case("ws_cmp_ordok", q_rev, "84", "IGNORED", [])
    assert "lower" in out_ord_ok["answer"].lower() and "116 percentage points" in out_ord_ok["answer"].lower()
    assert set(out_ord_ok["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}

    out_rel_ok = _run_case("ws_cmp_relok", q_rel, "84", "IGNORED", [])
    assert "138.1%" in out_rel_ok["answer"] and "relative change" in out_rel_ok["answer"].lower()
    assert set(out_rel_ok["final_citation_chunk_ids"]) == {"pdf_bal", "csv_h36"}

    # B. Prose validator unit checks (if model prose is ever evaluated, contradicting direction/delta/scale is blocked)
    from services.retrieval.deterministic_executor import verify_two_operand_comparison
    sgs_fwd = [{"id": "sub_1", "query_text": "Acme's June 30, 2026 current ratio"}, {"id": "sub_2", "query_text": "the 2025 Retail H36 value."}]
    sgs_rev = [{"id": "sub_1", "query_text": "the 2025 Retail H36 value"}, {"id": "sub_2", "query_text": "Acme's June 30, 2026 current ratio."}]
    chs_84 = [
        Chunk(id="pdf_bal", document_id="doc_pdf", element_type="Table", text="Acme Corp Condensed Consolidated Balance Sheet June 30, 2026. Total current assets $200,000. Total current liabilities $100,000.", page_number=5, source_format="pdf", workspace_id="w"),
        Chunk(id="csv_h36", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: 84.", page_number=1, source_format="csv", workspace_id="w"),
    ]
    chs_200 = [
        chs_84[0],
        Chunk(id="csv_h36", document_id="doc_csv", element_type="TableRow", text="Year: 2025. Industry: Retail. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: 200.", page_number=1, source_format="csv", workspace_id="w"),
    ]
    assert not verify_two_operand_comparison(q_fwd, sgs_fwd, chs_84, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 84%. Acme's ratio is lower than Retail H36.")[0]
    assert verify_two_operand_comparison(q_fwd, sgs_fwd, chs_84, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 84%. Acme's ratio is higher by 116 percentage points.")[0]
    assert not verify_two_operand_comparison(q_fwd, sgs_fwd, chs_84, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 84%. Acme's ratio is higher by 95 percentage points.")[0]
    assert not verify_two_operand_comparison(q_fwd, sgs_fwd, chs_200, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 200%. Acme's ratio is higher than Retail H36.")[0]
    assert verify_two_operand_comparison(q_fwd, sgs_fwd, chs_200, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 200%, equal to Acme's current ratio.")[0]
    assert not verify_two_operand_comparison(q_rev, sgs_rev, chs_84, "Retail H36 is 84% and Acme's current ratio is 200% (200,000 / 100,000). Retail H36 is higher than Acme's ratio.")[0]
    assert verify_two_operand_comparison(q_rev, sgs_rev, chs_84, "Retail H36 is 84% and Acme's current ratio is 200% (200,000 / 100,000). Retail H36 is lower by 116 percentage points.")[0]
    assert verify_two_operand_comparison(q_fwd, sgs_fwd, chs_84, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 84%. Acme's current ratio is 138.10% higher.")[0]
    assert not verify_two_operand_comparison(q_fwd, sgs_fwd, chs_84, "Acme's current ratio is 200% (200,000 / 100,000) and Retail H36 is 84%. Acme's ratio is higher by 138.10 percentage points.")[0]


def test_verified_two_operand_comparison_renders_deterministically_without_llm(monkeypatch):
    """Supported two-operand numeric comparisons render deterministically with citations and never invoke call_llm."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    _BM25_CACHE.clear()
    ws = "ws_det_cmp_unseen"
    chs = [
        Chunk(
            id="horizon_bal",
            document_id="doc_horizon",
            element_type="Table",
            text="Horizon Mfg Condensed Balance Sheet June 30, 2026. Total current assets $360,000. Total current liabilities $200,000.",
            page_number=4,
            source_format="pdf",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="sector_h36",
            document_id="doc_sector",
            element_type="TableRow",
            text="Year: 2025. Industry: Manufacturing. Units: Percentage. Variable_code: H36. Variable_name: Current ratio. Value: 95.",
            page_number=1,
            source_format="csv",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
    ]
    docs = [{"id": "doc_horizon", "filename": "horizon_10q.pdf"}, {"id": "doc_sector", "filename": "sector_survey.csv"}]

    class _Repo:
        def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs
        def get(self, doc_id): return None
        def search_vector(self, *a, **kw): return []
        def check_semantic_cache(self, *a, **kw): return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace(ws, "tester", workspace_id=ws)
    for d in docs:
        b_repo.create_source(ws, d["filename"], source_id=d["id"])
        b_repo.save_document_snapshot(DocumentSnapshot(
            workspace_id=ws, document_id=d["id"], source=d["filename"], version="v1",
            format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z",
        ))

    def _fail_if_called(*a, **kw):
        raise AssertionError("call_llm must not be called when two verified operands bind deterministically")

    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)
    with (
        patch("services.langgraph_pipeline.call_llm", side_effect=_fail_if_called),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="Using horizon_10q.pdf and sector_survey.csv, compare Horizon's June 30, 2026 current ratio with the 2025 Manufacturing H36 value.",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert out["generation_calls"] == 0
    assert out["answer"] == "Horizon's June 30, 2026 current ratio (180%) is 85 percentage points higher than the 2025 Manufacturing H36 value (95%)."
    assert set(out["final_citation_chunk_ids"]) == {"horizon_bal", "sector_h36"}

    # Unverified operand on supported numeric comparison must stop before synthesis and report which operand failed
    _BM25_CACHE.clear()
    ws_miss = "ws_det_cmp_unseen_missing"
    chs_missing_h36 = [
        Chunk(
            id="horizon_bal_m",
            document_id="doc_horizon",
            element_type="Table",
            text="Horizon Mfg Condensed Balance Sheet June 30, 2026. Total current assets $360,000. Total current liabilities $200,000.",
            page_number=4,
            source_format="pdf",
            workspace_id=ws_miss,
            version="v1",
            document_version="v1",
        )
    ]
    class _RepoMissing(_Repo):
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs_missing_h36
    b_repo_miss = InMemoryWorkspaceBoundaryRepository()
    b_repo_miss.create_workspace(ws_miss, "tester", workspace_id=ws_miss)
    for d in docs:
        b_repo_miss.create_source(ws_miss, d["filename"], source_id=d["id"])
        b_repo_miss.save_document_snapshot(DocumentSnapshot(
            workspace_id=ws_miss, document_id=d["id"], source=d["filename"], version="v1",
            format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z",
        ))
    repo_miss = _RepoMissing()
    qs_miss = QueryService(repo=repo_miss)
    with (
        patch("services.langgraph_pipeline.call_llm", side_effect=_fail_if_called),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_miss),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo_miss),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo_miss),
    ):
        out_miss = qs_miss.execute_query(QueryRequest(
            query="Using horizon_10q.pdf and sector_survey.csv, compare Horizon's June 30, 2026 current ratio with the 2025 Manufacturing H36 value.",
            workspace_id=ws_miss,
            cache=False,
            benchmark_mode=True,
        ))
    assert out_miss["generation_calls"] == 0
    assert out_miss["citations"] == []
    assert "couldn't find" in out_miss["answer"].lower()
    assert len(out_miss["unverified_operands"]) == 1
    assert out_miss["unverified_operands"][0]["subgoal_id"] == "sub_2"

    # Qualitative comparison outside supported numeric path keeps normal synthesis available
    _BM25_CACHE.clear()
    ws_qual = "ws_det_cmp_unseen_qual"
    chs_qual = [
        Chunk(
            id="horizon_gov",
            document_id="doc_horizon",
            element_type="NarrativeText",
            text="Horizon governance disclosures emphasize board oversight and audit independence.",
            page_number=2,
            source_format="pdf",
            workspace_id=ws_qual,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="sector_meth",
            document_id="doc_sector",
            element_type="NarrativeText",
            text="Survey methodology covers stratified sampling across manufacturing establishments.",
            page_number=1,
            source_format="csv",
            workspace_id=ws_qual,
            version="v1",
            document_version="v1",
        ),
    ]
    class _RepoQual(_Repo):
        def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs_qual
    b_repo_qual = InMemoryWorkspaceBoundaryRepository()
    b_repo_qual.create_workspace(ws_qual, "tester", workspace_id=ws_qual)
    for d in docs:
        b_repo_qual.create_source(ws_qual, d["filename"], source_id=d["id"])
        b_repo_qual.save_document_snapshot(DocumentSnapshot(
            workspace_id=ws_qual, document_id=d["id"], source=d["filename"], version="v1",
            format=d["filename"].split(".")[-1], created_at="2026-11-26T00:00:00Z",
        ))
    repo_qual = _RepoQual()
    qs_qual = QueryService(repo=repo_qual)
    llm_qual_calls = []
    def _qual_llm(*a, **kw):
        llm_qual_calls.append(1)
        return {"answer": "Both documents cover governance disclosures and survey methodology.", "citations": ["horizon_gov", "sector_meth"], "usage": {"input_tokens": 5, "output_tokens": 5}}
    with (
        patch("services.langgraph_pipeline.call_llm", side_effect=_qual_llm),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_qual),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo_qual),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo_qual),
    ):
        out_qual = qs_qual.execute_query(QueryRequest(
            query="Using horizon_10q.pdf and sector_survey.csv, compare governance disclosures with survey methodology.",
            workspace_id=ws_qual,
            cache=False,
            benchmark_mode=True,
        ))
    assert len(llm_qual_calls) == 1
    assert out_qual["unverified_operands"] == []





