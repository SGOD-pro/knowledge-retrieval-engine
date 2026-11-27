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


def test_premise_correction_two_sentence_and_no_evidence_control(monkeypatch):
    """Two-sentence premise-correction queries deliver cited answers while no-evidence queries refuse."""
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

    ws = "ws_premise_ctrl"
    chs = [
        Chunk(
            id="obs_row_1",
            document_id="doc_obs",
            element_type="TableRow",
            text="Station: Coastal North | Metric: Humidity | Value: -14.2 | Unit: percent",
            page_number=1,
            source_format="csv",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="act_row_2",
            document_id="doc_act",
            element_type="TableRow",
            text="Title: Coastal Protection Act, 2021 | EnactedYear: 2019 | Introduced: 04/11/2019",
            page_number=1,
            source_format="csv",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
    ]
    docs = [{"id": "doc_obs", "filename": "weather_obs.csv"}, {"id": "doc_act", "filename": "statute_log.csv"}]

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
            format="csv", created_at="2026-11-26T00:00:00Z",
        ))

    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)

    _BM25_CACHE.clear()
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "Coastal North stores -14.2%, which is negative and unsafe to interpret literally.",
            "citations": ["obs_row_1"],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_prem1 = qs.execute_query(QueryRequest(
            query="The weather log says Coastal North had 14.2% humidity. Confirm the exact stored value and whether it is safe to interpret literally.",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "-14.2%" in out_prem1["answer"]
    assert out_prem1["final_citation_chunk_ids"] == ["obs_row_1"]

    _BM25_CACHE.clear()
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "No, the Coastal Protection Act, 2021 was introduced on 04/11/2019, not in 2021.",
            "citations": ["act_row_2"],
            "usage": {"input_tokens": 10, "output_tokens": 10},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_prem2 = qs.execute_query(QueryRequest(
            query="The Coastal Protection Act has 2021 in its title, so the log must record its introduction in 2021. Is that correct?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "04/11/2019" in out_prem2["answer"]
    assert out_prem2["final_citation_chunk_ids"] == ["act_row_2"]

    _BM25_CACHE.clear()
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={"answer": "NOT_FOUND", "citations": [], "usage": {}}),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out_no_ev = qs.execute_query(QueryRequest(
            query="What is the annual rainfall in Desert Basin according to weather_obs.csv?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))
    assert "couldn't find" in out_no_ev["answer"].lower()
    assert out_no_ev["citations"] == []


def test_scorer_ignores_sequential_list_markers_but_checks_real_claims():
    """Scorer 2.10.0 ignores 1) 2) 3) 4) list markers while still rejecting unsupported numeric claims even if they repeat a list number."""
    from services.evaluation.benchmark_scorer import SCORER_VERSION, evaluate_grounded_answer

    assert SCORER_VERSION == "2.10.0"
    cit = [{
        "chunk_id": "doc_p1",
        "document_id": "d1",
        "document_filename": "paper.pdf",
        "document_version": "v1",
        "page_number": 1,
        "source_format": "pdf",
        "text": "The four phases of learning are Alpha, Beta, Gamma, and Delta.",
        "text_snippet": "The four phases of learning are Alpha, Beta, Gamma, and Delta.",
    }]
    ev = [{
        "evidence_id": "doc_p1",
        "locator": {"chunk_id": "doc_p1"},
        "text": cit[0]["text"],
        "provenance": {"source": "paper.pdf", "version": "v1"},
    }]
    res_ok = evaluate_grounded_answer(
        answer="The four phases are: 1) Alpha, 2) Beta, 3) Gamma, and 4) Delta. [doc_p1]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["paper.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["doc_p1"],
        ground_truth_chunk_ids=["doc_p1"],
        query="Name the four phases of learning in order.",
    )
    assert res_ok["grounded_correct"] is True

    res_bad_repeat = evaluate_grounded_answer(
        answer="The phases are: 1) Alpha takes 2 hours, 2) Beta, 3) Gamma, and 4) Delta. [doc_p1]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["paper.pdf"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["doc_p1"],
        ground_truth_chunk_ids=["doc_p1"],
        query="Name the four phases of learning in order.",
    )
    assert res_bad_repeat["grounded_correct"] is False
    assert "numeric claims" in res_bad_repeat["reason"]


def test_premise_correction_title_vs_intro_year_and_followup_control():
    """Scorer allows disambiguating title vs introduction year while rejecting wrong years or title-year denials; real follow-ups still decompose."""
    from services.evaluation.benchmark_scorer import evaluate_grounded_answer

    cit = [{
        "chunk_id": "stat_r2",
        "document_id": "d_stat",
        "document_filename": "statute_log.csv",
        "document_version": "v1",
        "location_reference": "Row 2",
        "source_format": "csv",
        "text": "Year: 2019. Short Title: Coastal Protection Act, 2021. Date of Introduction: 04/11/2019. Deadline for Introduction Amendments: 12/06/2023. Status: Assented.",
        "text_snippet": "Year: 2019. Short Title: Coastal Protection Act, 2021. Date of Introduction: 04/11/2019. Deadline for Introduction Amendments: 12/06/2023. Status: Assented.",
    }]
    ev = [{
        "evidence_id": "stat_r2",
        "locator": {"chunk_id": "stat_r2"},
        "text": cit[0]["text"],
        "provenance": {"source": "statute_log.csv", "version": "v1"},
    }]

    res_ok = evaluate_grounded_answer(
        answer="No, the Coastal Protection Act, 2021 was introduced on 04/11/2019, not in 2021. [stat_r2]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["statute_log.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["stat_r2"],
        ground_truth_chunk_ids=["stat_r2"],
        query="The Coastal Protection Act has 2021 in its title, so it must have been introduced in 2021. Correct?",
    )
    assert res_ok["grounded_correct"] is True

    res_wrong_year = evaluate_grounded_answer(
        answer="No, the Coastal Protection Act, 2021 was introduced in 2020, not in 2021. [stat_r2]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["statute_log.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["stat_r2"],
        ground_truth_chunk_ids=["stat_r2"],
        query="The Coastal Protection Act has 2021 in its title, so it must have been introduced in 2021. Correct?",
    )
    assert res_wrong_year["grounded_correct"] is False
    assert "claim_contradicts_evidence" in res_wrong_year["reason"]

    # 3-year control: substituting the passage/debate year (2023) for the introduction year (2019) must fail
    res_passage_year = evaluate_grounded_answer(
        answer="No, the Coastal Protection Act, 2021 was introduced in 2023, not in 2021. [stat_r2]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["statute_log.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["stat_r2"],
        ground_truth_chunk_ids=["stat_r2"],
        query="The Coastal Protection Act has 2021 in its title, so it must have been introduced in 2021. Correct?",
    )
    assert res_passage_year["grounded_correct"] is False
    assert "claim_contradicts_evidence" in res_passage_year["reason"]

    res_deny_title = evaluate_grounded_answer(
        answer="The Coastal Protection Act does not have 2021 in its title. [stat_r2]",
        citations=cit,
        is_answer_text_correct=True,
        expected_sources=["statute_log.csv"],
        expected_source_version="v1",
        retained_evidence_items=ev,
        context_chunk_ids=["stat_r2"],
        ground_truth_chunk_ids=["stat_r2"],
        query="Does the Coastal Protection Act have 2021 in its title?",
    )
    assert res_deny_title["grounded_correct"] is False
    assert "claim_contradicts_evidence" in res_deny_title["reason"]

    # Control: a real second requirement without a year, metric code, or filename must still decompose into 2 subgoals
    plan_real_followup = decompose_query("The model uses attention pooling. Also explain its memory limitation.")
    assert len(plan_real_followup.subgoals) == 2


def test_multi_claim_missing_citation_fallback_and_wrong_claim_rejection(monkeypatch):
    """When two claims have support in context and model cites one, fallback adds the second supporting chunk or withholds if wrong claim."""
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

    docs = [{"id": "doc_pol", "filename": "policy_guide.pdf"}, {"id": "doc_eval", "filename": "eval_study.pdf"}]
    q_text = (
        "The regional policy requires auditable medical triage while preserving diagnostic speed. "
        "What finding from the evaluation study shows why headline accuracy alone cannot guarantee safe triage?"
    )
    model_ans = (
        "The regional policy requires auditable medical triage while preserving diagnostic speed. "
        "The evaluation study shows that a triage classifier can reach 98% headline accuracy while misrouting critical cases."
    )

    def _run(ws: str, chs: list[Chunk]):
        _BM25_CACHE.clear()
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
                format="pdf", created_at="2026-11-26T00:00:00Z",
            ))
        repo_inst = _Repo()
        qs = QueryService(repo=repo_inst)
        with (
            patch("services.langgraph_pipeline.call_llm", return_value={
                "answer": model_ans,
                "citations": ["eval_c1"],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }),
            patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(QueryRequest(query=q_text, workspace_id=ws, cache=False, benchmark_mode=True))

    # Case A: Both supporting chunks in context -> verified fallback adds pol_c1 alongside eval_c1
    ws_ok = "ws_mc_ok"
    chs_ok = [
        Chunk(
            id="pol_c1",
            document_id="doc_pol",
            element_type="NarrativeText",
            text="Regional medical policy mandates auditable triage decisions while preserving diagnostic speed.",
            page_number=12,
            source_format="pdf",
            workspace_id=ws_ok,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="eval_c1",
            document_id="doc_eval",
            element_type="NarrativeText",
            text="The evaluation study shows a triage classifier can reach 98% headline accuracy while misrouting critical cases.",
            page_number=5,
            source_format="pdf",
            workspace_id=ws_ok,
            version="v1",
            document_version="v1",
        ),
    ]
    out_ok = _run(ws_ok, chs_ok)
    assert set(out_ok["final_citation_chunk_ids"]) == {"pol_c1", "eval_c1"}

    # Case B: policy_guide.pdf chunk supports a different claim (billing audit, not medical triage speed) -> withheld
    ws_wrong = "ws_mc_wrong"
    chs_wrong = [
        Chunk(
            id="pol_wrong",
            document_id="doc_pol",
            element_type="NarrativeText",
            text="Regional policy covers quarterly billing audits for outpatient clinics.",
            page_number=3,
            source_format="pdf",
            workspace_id=ws_wrong,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="eval_c1",
            document_id="doc_eval",
            element_type="NarrativeText",
            text="The evaluation study shows a triage classifier can reach 98% headline accuracy while misrouting critical cases.",
            page_number=5,
            source_format="pdf",
            workspace_id=ws_wrong,
            version="v1",
            document_version="v1",
        ),
    ]
    out_wrong = _run(ws_wrong, chs_wrong)
    assert "couldn't find" in out_wrong["answer"].lower()
    assert out_wrong["citations"] == []


def test_single_subgoal_missing_citation_recovery_and_wrong_claim_rejection(monkeypatch):
    """Single-subgoal queries recover an omitted supporting chunk from retained context while rejecting overlapping wrong-claim chunks."""
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

    docs = [{"id": "doc_algo", "filename": "fast_attn.pdf"}]
    q_text = "Because Fast-Attn inference is independent of sequence length, must its computation also be independent of channel count? Correct?"
    model_ans = (
        "No, Fast-Attn inference is independent of sequence length, "
        "but its O(kC) inference cost depends on channel count C and kernel order k."
    )

    def _run(ws: str, chs: list[Chunk]):
        _BM25_CACHE.clear()
        class _Repo:
            def get_workspace_documents(self, workspace_id, page=1, limit=1000): return {"documents": docs}
            def get_all_chunks(self, document_ids=None, workspace_id=None, **kw): return chs
            def get(self, doc_id): return None
            def search_vector(self, *a, **kw): return []
            def check_semantic_cache(self, *a, **kw): return None
        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace(ws, "tester", workspace_id=ws)
        b_repo.create_source(ws, "fast_attn.pdf", source_id="doc_algo")
        b_repo.save_document_snapshot(DocumentSnapshot(
            workspace_id=ws, document_id="doc_algo", source="fast_attn.pdf", version="v1",
            format="pdf", created_at="2026-11-26T00:00:00Z",
        ))
        repo_inst = _Repo()
        qs = QueryService(repo=repo_inst)
        with (
            patch("services.langgraph_pipeline.call_llm", return_value={
                "answer": model_ans,
                "citations": ["algo_p6"],
                "usage": {"input_tokens": 10, "output_tokens": 10},
            }),
            patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(QueryRequest(query=q_text, workspace_id=ws, cache=False, benchmark_mode=True))

    # Case A: algo_p5 in retained context supports Fast-Attn sequence length independence -> recovered
    ws_rec = "ws_sg_rec"
    chs_rec = [
        Chunk(
            id="algo_p6",
            document_id="doc_algo",
            element_type="NarrativeText",
            text="Fast-Attn achieves O(kC) inference cost depending on channel count C and kernel order k.",
            page_number=6,
            source_format="pdf",
            workspace_id=ws_rec,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="algo_p5",
            document_id="doc_algo",
            element_type="NarrativeText",
            text="Fast-Attn updates constant-size caches so inference is independent of sequence length.",
            page_number=5,
            source_format="pdf",
            workspace_id=ws_rec,
            version="v1",
            document_version="v1",
        ),
    ]
    out_rec = _run(ws_rec, chs_rec)
    assert set(out_rec["final_citation_chunk_ids"]) == {"algo_p6", "algo_p5"}

    # Case B: algo_wrong has overlapping terms ("inference", "sequence length", "independent") but opposite claim -> NOT added
    ws_wrong = "ws_sg_wrong"
    chs_wrong = [
        Chunk(
            id="algo_p6",
            document_id="doc_algo",
            element_type="NarrativeText",
            text="Fast-Attn achieves O(kC) inference cost depending on channel count C and kernel order k.",
            page_number=6,
            source_format="pdf",
            workspace_id=ws_wrong,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="algo_wrong",
            document_id="doc_algo",
            element_type="NarrativeText",
            text="Fast-Attn inference is not independent of sequence length under sliding windows.",
            page_number=7,
            source_format="pdf",
            workspace_id=ws_wrong,
            version="v1",
            document_version="v1",
        ),
    ]
    out_wrong = _run(ws_wrong, chs_wrong)
    assert out_wrong["final_citation_chunk_ids"] == ["algo_p6"]


def test_compression_distant_sentence_stop_reason_and_explain_using_both_papers(monkeypatch):
    """Compression preserves distant decisive sentences within budget; max_tokens stop_reason is refused; 'Explain using both papers.' delivers."""
    from unittest.mock import patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.retrieval.bm25_retriever import _BM25_CACHE
    from services.retrieval.compressor import compress_chunks

    # 1. Compression control: distant decisive sentence preserved within budget_per_chunk
    filler = " ".join(["Background methodology details are summarized in appendix sections."] * 18)
    long_chunk = Chunk(
        id="long_c1",
        document_id="doc_long",
        element_type="NarrativeText",
        text=f"Alpha caching keeps inference cost constant with sequence length. {filler} Beta polynomial order t determines the O(tD) cache update.",
        page_number=5,
        source_format="pdf",
        workspace_id="ws_comp",
        version="v1",
        document_version="v1",
    )
    comp_out = compress_chunks("What does alpha caching do and how does beta polynomial order affect O(tD)?", [long_chunk])
    assert "Alpha caching" in comp_out
    assert "Beta polynomial order" in comp_out

    # 2 & 3. Stop-reason control (end_turn vs max_tokens) + "Explain using both papers." control
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("BEDROCK_RERANKER_ENABLED", "false")
    monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", "false")

    docs = [{"id": "doc_a", "filename": "fast_attn.pdf"}, {"id": "doc_b", "filename": "memory_layer.pdf"}]
    q_text = "Does Fast-Attn constant-length cache establish persistent cross-session memory? Explain using both papers."
    ans_text = (
        "No, Fast-Attn uses constant-size caches for single-sequence inference, "
        "whereas Memory-Layer adds a persistent module for cross-session recall."
    )

    def _run_stop(ws: str, stop_reason: str):
        _BM25_CACHE.clear()
        chs = [
            Chunk(
                id="fa_1",
                document_id="doc_a",
                element_type="NarrativeText",
                text="Fast-Attn uses constant-size caches for single-sequence inference.",
                page_number=5,
                source_format="pdf",
                workspace_id=ws,
                version="v1",
                document_version="v1",
            ),
            Chunk(
                id="ml_1",
                document_id="doc_b",
                element_type="NarrativeText",
                text="Memory-Layer adds a persistent module for cross-session recall across interactions.",
                page_number=1,
                source_format="pdf",
                workspace_id=ws,
                version="v1",
                document_version="v1",
            ),
        ]
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
                format="pdf", created_at="2026-11-26T00:00:00Z",
            ))
        repo_inst = _Repo()
        qs = QueryService(repo=repo_inst)
        with (
            patch("services.langgraph_pipeline.call_llm", return_value={
                "answer": ans_text,
                "citations": ["fa_1", "ml_1"],
                "usage": {"input_tokens": 120, "output_tokens": 42, "stop_reason": stop_reason},
            }),
            patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        ):
            return qs.execute_query(QueryRequest(query=q_text, workspace_id=ws, cache=False, benchmark_mode=True))

    out_ok = _run_stop("ws_stop_ok", "end_turn")
    assert set(out_ok["final_citation_chunk_ids"]) == {"fa_1", "ml_1"}
    assert out_ok["retrieval_candidates"]["llm_stop_reason"] == "end_turn"
    assert out_ok["retrieval_candidates"]["llm_output_tokens"] == 42

    out_trunc = _run_stop("ws_stop_trunc", "max_tokens")
    assert "couldn't find" in out_trunc["answer"].lower()
    assert out_trunc["citations"] == []
    assert out_trunc["retrieval_candidates"]["llm_stop_reason"] == "max_tokens"
    assert out_trunc["retrieval_candidates"]["llm_output_tokens"] == 42
    assert out_trunc["retrieval_candidates"]["delivery_rejection_reason"] == "truncated_stop_reason:max_tokens"


def test_sqlite_create_source_idempotent_and_preserves_tombstone(tmp_path):
    from db.workspace_boundary_repo import SQLiteWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot

    repo = SQLiteWorkspaceBoundaryRepository(str(tmp_path / "boundary.db"))
    repo.create_workspace("Dup Workspace", "usr_alice", workspace_id="ws_dup")

    first = repo.create_source("ws_dup", "facility_report.pdf", source_id="src_1")
    second = repo.create_source("ws_dup", "facility_report.pdf", source_id="src_1")
    assert first.source_id == second.source_id == "src_1"
    rows = repo._conn.execute(
        "SELECT source_id, status FROM sources WHERE workspace_id = ? AND source_id = ?",
        ("ws_dup", "src_1"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "registered"

    repo.save_document_snapshot(DocumentSnapshot(
        workspace_id="ws_dup",
        document_id="src_1",
        source="facility_report.pdf",
        version="v1",
        format="pdf",
        created_at="2026-11-27T00:00:00Z",
        persisted_rows=12,
        total_rows=12,
    ))
    snap = repo.get_document_snapshot("ws_dup", "src_1")
    assert snap is not None
    assert snap.version == "v1"
    assert snap.persisted_rows == 12
    assert snap.total_rows == 12

    repo.record_source_tombstone("ws_dup", "src_revoked", "usr_alice", "revoked")
    with pytest.raises(PermissionError):
        repo.create_source("ws_dup", "revoked.pdf", source_id="src_revoked")


def test_bare_doc_uuid_citation_tag_recovers_in_verified_fallback(monkeypatch):
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

    ws = "ws_bare_uuid"
    _BM25_CACHE.clear()
    docs = [{"id": "alpha-doc-uuid-1111", "filename": "facility_brief.pdf"}]
    chs = [
        Chunk(
            id="alpha_p1_0",
            document_id="alpha-doc-uuid-1111",
            element_type="NarrativeText",
            text="The Q3 coolant pressure target was 42.5 psi across all loops.",
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
    b_repo.create_source(ws, "facility_brief.pdf", source_id="alpha-doc-uuid-1111")
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws, document_id="alpha-doc-uuid-1111", source="facility_brief.pdf", version="v1",
        format="pdf", created_at="2026-11-27T00:00:00Z",
    ))
    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "The Q3 coolant pressure target was 42.5 psi. [alpha-doc-uuid-1111]",
            "citations": ["alpha-doc-uuid-1111"],
            "usage": {"input_tokens": 50, "output_tokens": 15, "stop_reason": "end_turn"},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="What was the Q3 coolant pressure target in the facility brief?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert "42.5" in out["answer"]
    assert out["final_citation_chunk_ids"] == ["alpha_p1_0"]


def test_cross_doc_explanation_allows_same_doc_sibling_year_in_fallback(monkeypatch):
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

    ws = "ws_sibling_year"
    _BM25_CACHE.clear()
    docs = [{"id": "doc_ops", "filename": "ops_plan.pptx"}, {"id": "doc_eng", "filename": "eng_log.pdf"}]
    chs = [
        Chunk(
            id="ops_title",
            document_id="doc_ops",
            element_type="Title",
            text="2027 Operations Plan",
            page_number=1,
            source_format="pptx",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="ops_body",
            document_id="doc_ops",
            element_type="NarrativeText",
            text="Cooling power rose 18 percent to offset warmer ambient air.",
            page_number=1,
            source_format="pptx",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="eng_title",
            document_id="doc_eng",
            element_type="Title",
            text="2027 Engineering Log",
            page_number=1,
            source_format="pdf",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
        Chunk(
            id="eng_body",
            document_id="doc_eng",
            element_type="NarrativeText",
            text="Ambient inlet reached -12 degrees Celsius compared with -15 degrees Celsius baseline.",
            page_number=2,
            source_format="pdf",
            workspace_id=ws,
            version="v1",
            document_version="v1",
        ),
    ]

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
            format="pdf", created_at="2026-11-27T00:00:00Z",
        ))
    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)
    ans = (
        "In 2027, cooling power rose 18 percent to offset warmer ambient air, "
        "and ambient inlet reached -12 degrees Celsius compared with -15 degrees Celsius baseline."
    )
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": ans,
            "citations": ["ops_title", "eng_title"],
            "usage": {"input_tokens": 80, "output_tokens": 30, "stop_reason": "end_turn"},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="Did 2027 cooling power rise to offset warmer ambient air when ambient inlet reached -12 degrees Celsius? Explain using both documents.",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert "18" in out["answer"]
    assert "-12" in out["answer"]
    assert "ops_body" in out["final_citation_chunk_ids"]
    assert "eng_body" in out["final_citation_chunk_ids"]


def test_numeric_false_premise_bare_no_enriched_with_cited_correction(monkeypatch):
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

    ws = "ws_bare_no"
    _BM25_CACHE.clear()
    docs = [{"id": "doc_log", "filename": "eng_log.pdf"}]
    chs = [
        Chunk(
            id="eng_body",
            document_id="doc_log",
            element_type="NarrativeText",
            text="Ambient inlet reached -12 degrees Celsius during the night cycle.",
            page_number=2,
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
    b_repo.create_source(ws, "eng_log.pdf", source_id="doc_log")
    b_repo.save_document_snapshot(DocumentSnapshot(
        workspace_id=ws, document_id="doc_log", source="eng_log.pdf", version="v1",
        format="pdf", created_at="2026-11-27T00:00:00Z",
    ))
    repo_inst = _Repo()
    qs = QueryService(repo=repo_inst)
    with (
        patch("services.langgraph_pipeline.call_llm", return_value={
            "answer": "No, that is not correct.",
            "citations": ["eng_body"],
            "usage": {"input_tokens": 40, "output_tokens": 8, "stop_reason": "end_turn"},
        }),
        patch("services.langgraph_pipeline._get_repo", return_value=repo_inst),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
    ):
        out = qs.execute_query(QueryRequest(
            query="Is it correct that the ambient inlet reached +12 degrees Celsius?",
            workspace_id=ws,
            cache=False,
            benchmark_mode=True,
        ))

    assert "-12 degrees Celsius" in out["answer"]
    assert out["final_citation_chunk_ids"] == ["eng_body"]







