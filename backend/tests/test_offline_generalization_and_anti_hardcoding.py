"""Offline anti-hardcoding and generalization verification.

Audits production retrieval, ranking, synthesis prompts, delivery gate, and scorer
for benchmark-specific identifiers and verifies generalization on fresh fixtures:
- Different company: Acme Industrial Corp.
- Different metric: Operating income
- Different entity: Products / Services
- Different dates: September 30, 2027 and September 30, 2026
- Different column order: Nine Months Ended first, then Three Months Ended
- Different unit scale: dollars in thousands (vs millions)
- Different source ID and document filename: acme-10q-2027.pdf
- Correct answer passes; neighboring row and period column values are rejected
- Changing source amount changes the verified answer
- Nonfinancial control query passes without financial table errors
"""

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from schemas.contracts.boundary import DocumentSnapshot
from schemas.models import Chunk
from services.evaluation.benchmark_scorer import evaluate_grounded_answer
from services.langgraph_pipeline import delivery_gate, run_llm
from services.retrieval.evidence_contract import evidence_item_from_chunk
from services.retrieval.response_builder import build_citation
from services.retrieval.table_metric_binding import (
    bind_financial_query,
    verify_financial_cell_binding,
)

WS_ID = "ws_acme_generalization_test"
DOC_ID = "acme_10q_doc_9999"
CHUNK_ID = f"{DOC_ID}:page:12:element:5"
SOURCE_FILENAME = "acme-10q-2027.pdf"
SNAPSHOT_VERSION = "v_acme_snap_1"

# Fresh fixture: Acme Industrial Corp with Nine Months columns first, then Three Months
ACME_TABLE_TEXT = """Acme Industrial Corp.
CONSOLIDATED STATEMENTS OF OPERATIONS (dollars in thousands)
Nine Months Ended Three Months Ended
September 30, 2027 September 30, 2026 September 30, 2027 September 30, 2026
Operating income:
   Products $ 42,600 $ 35,100 $ 14,820 $ 11,200
   Services 25,400 21,300 8,450 7,100
Total operating income $ 68,000 $ 56,400 $ 23,270 $ 18,300
"""

ACME_TABLE_TEXT_CHANGED = """Acme Industrial Corp.
CONSOLIDATED STATEMENTS OF OPERATIONS (dollars in thousands)
Nine Months Ended Three Months Ended
September 30, 2027 September 30, 2026 September 30, 2027 September 30, 2026
Operating income:
   Products $ 42,600 $ 35,100 $ 16,500 $ 11,200
   Services 25,400 21,300 8,450 7,100
Total operating income $ 68,000 $ 56,400 $ 24,950 $ 18,300
"""


def _make_acme_boundary_repo() -> InMemoryWorkspaceBoundaryRepository:
    repo = InMemoryWorkspaceBoundaryRepository()
    repo.create_workspace("Acme Generalization", "owner_1", WS_ID)
    repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=WS_ID,
            document_id=DOC_ID,
            source=SOURCE_FILENAME,
            version=SNAPSHOT_VERSION,
            format="pdf",
            created_at="2027-10-01T00:00:00Z",
        )
    )
    return repo


def _make_acme_chunk(text: str = ACME_TABLE_TEXT) -> Chunk:
    return Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        source_format="pdf",
        text=text,
        element_type="table",
        page_number=12,
        bounding_box={"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "page_number": 12},
        location_reference="Page: 12",
        workspace_id=WS_ID,
        version=SNAPSHOT_VERSION,
        document_version=SNAPSHOT_VERSION,
        metadata={"document_filename": SOURCE_FILENAME, "version": SNAPSHOT_VERSION},
        reranker_score=0.95,
    )


def test_audit_production_code_for_benchmark_hardcoding():
    """Audit production source files for benchmark question IDs and hardcoded expectations."""
    src_dir = Path(__file__).resolve().parent.parent / "src"

    # Files to audit
    files_to_check = [
        src_dir / "services" / "retrieval" / "table_metric_binding.py",
        src_dir / "services" / "retrieval" / "reranker.py",
        src_dir / "services" / "retrieval" / "response_builder.py",
        src_dir / "services" / "llm" / "llm_service.py",
        src_dir / "services" / "langgraph_pipeline.py",
        src_dir / "services" / "evaluation" / "benchmark_scorer.py",
        src_dir / "providers" / "reranker_provider.py",
    ]

    benchmark_ids = ["Q193", "Q136", "Q138", "Q150", "Q167", "Q184"]

    runtime_hits = []
    for fpath in files_to_check:
        assert fpath.exists(), f"Expected production file {fpath} to exist"
        lines = fpath.read_text(encoding="utf-8").splitlines()
        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            # Ignore comments
            if stripped.startswith("#"):
                continue
            for q_id in benchmark_ids:
                if q_id in line:
                    runtime_hits.append((str(fpath.name), idx, q_id, line.strip()))

    # No benchmark question ID may appear in runtime executable code
    assert runtime_hits == [], f"Found benchmark question IDs in runtime code: {runtime_hits}"


def test_pipeline_generalization_on_fresh_acme_fixtures():
    """Verify normal pipeline and scorer with fresh company, dates, columns, scale, and source IDs."""
    repo = _make_acme_boundary_repo()
    chunk = _make_acme_chunk()
    ev = evidence_item_from_chunk(chunk, workspace_id=WS_ID, strategy="bm25", boundary_repo=repo)

    query = "What was Acme Industrial Corp Products operating income for the three months ended September 30, 2027?"
    binding = bind_financial_query(query)
    assert binding.is_active is True
    assert binding.metric == "operating_income"
    assert binding.entity == "Products"
    assert binding.duration_norm == "three_months"
    assert binding.date_parsed == (2027, 9, 30)

    base_state = {
        "query": query,
        "workspace_id": WS_ID,
        "boundary_repo": repo,
        "compressed_text": f"[{CHUNK_ID}]\n{ACME_TABLE_TEXT}",
        "top_chunks": [chunk],
        "retained_evidence_items": [ev],
        "financial_binding": binding,
        "retrieval_candidates": {"compressed_context": [CHUNK_ID]},
        "synthesis_calls": 0,
    }

    # 1. Correct answer passes pipeline delivery gate and grounded scorer
    good_answer = "Acme Products operating income was 14,820 thousand dollars for the three months ended September 30, 2027."
    good_payload = json.dumps({"answer": good_answer, "citations": [CHUNK_ID]})
    with patch("services.llm.llm_service.generate_completion", return_value=(good_payload, {"input_tokens": 100, "output_tokens": 20})):
        out_state = run_llm(dict(base_state))
        gate_state = {**base_state, **out_state}
        gate_state.update(delivery_gate(gate_state))

    assert gate_state["final_answer"] == good_answer
    assert len(gate_state["citations"]) == 1
    assert gate_state["citations"][0]["document_filename"] == SOURCE_FILENAME

    eval_good = evaluate_grounded_answer(
        answer=gate_state["final_answer"],
        citations=gate_state["citations"],
        is_answer_text_correct=True,
        contract_type="semantic",
        contract={"contract_type": "semantic", "expected_answer": "14,820 thousand dollars"},
        expected_sources=[SOURCE_FILENAME],
        expected_source_version=SNAPSHOT_VERSION,
        retained_evidence_items=[ev],
        context_chunk_ids=[CHUNK_ID],
        ground_truth_chunk_ids=[CHUNK_ID],
        query=query,
    )
    assert eval_good["is_grounded_correct"] is True
    assert eval_good["reason"] == "grounded_and_verified"

    # 2. Plausible wrong answer from neighboring row (Services 8,450) is rejected
    wrong_row_ans = "Acme Products operating income was 8,450 thousand dollars for the three months ended September 30, 2027."
    bad_payload = json.dumps({"answer": wrong_row_ans, "citations": [CHUNK_ID]})
    with patch("services.llm.llm_service.generate_completion", return_value=(bad_payload, {"input_tokens": 100, "output_tokens": 20})):
        out_bad = run_llm(dict(base_state))
        gate_bad = {**base_state, **out_bad}
        gate_bad.update(delivery_gate(gate_bad))

    assert gate_bad["final_answer"] == "NOT_FOUND"
    assert gate_bad["citations"] == []

    # 3. Plausible wrong answer from neighboring period column (prior-year 11,200) is rejected
    wrong_col_ans = "Acme Products operating income was 11,200 thousand dollars for the three months ended September 30, 2027."
    bad_col_payload = json.dumps({"answer": wrong_col_ans, "citations": [CHUNK_ID]})
    with patch("services.llm.llm_service.generate_completion", return_value=(bad_col_payload, {"input_tokens": 100, "output_tokens": 20})):
        out_bcol = run_llm(dict(base_state))
        gate_bcol = {**base_state, **out_bcol}
        gate_bcol.update(delivery_gate(gate_bcol))

    assert gate_bcol["final_answer"] == "NOT_FOUND"

    # 4. Plausible wrong answer from nine-month duration column (42,600) is rejected
    wrong_dur_ans = "Acme Products operating income was 42,600 thousand dollars for the three months ended September 30, 2027."
    bad_dur_payload = json.dumps({"answer": wrong_dur_ans, "citations": [CHUNK_ID]})
    with patch("services.llm.llm_service.generate_completion", return_value=(bad_dur_payload, {"input_tokens": 100, "output_tokens": 20})):
        out_bdur = run_llm(dict(base_state))
        gate_bdur = {**base_state, **out_bdur}
        gate_bdur.update(delivery_gate(gate_bdur))

    assert gate_bdur["final_answer"] == "NOT_FOUND"


def test_changing_source_amount_changes_verified_answer():
    """Verify that changing the source table amount directly changes which answer is verified."""
    query = "What was Acme Industrial Corp Products operating income for the three months ended September 30, 2027?"
    binding = bind_financial_query(query)

    old_answer = "Acme Products operating income was 14,820 thousand dollars for the three months ended September 30, 2027."
    new_answer = "Acme Products operating income was 16,500 thousand dollars for the three months ended September 30, 2027."

    # On original table (amount 14,820):
    ok_orig_old, _ = verify_financial_cell_binding(ACME_TABLE_TEXT, old_answer, binding)
    ok_orig_new, _ = verify_financial_cell_binding(ACME_TABLE_TEXT, new_answer, binding)
    assert ok_orig_old is True
    assert ok_orig_new is False

    # On modified table (amount 16,500):
    ok_mod_old, _ = verify_financial_cell_binding(ACME_TABLE_TEXT_CHANGED, old_answer, binding)
    ok_mod_new, _ = verify_financial_cell_binding(ACME_TABLE_TEXT_CHANGED, new_answer, binding)
    assert ok_mod_old is False
    assert ok_mod_new is True


def test_nonfinancial_control_generalization():
    """Verify that a nonfinancial control query passes delivery gate and scorer without financial table errors."""
    repo = _make_acme_boundary_repo()
    nf_chunk_id = f"{DOC_ID}:page:1:element:1"
    nf_text = "The annual shareholder meeting was held on October 15, 2027 at company headquarters in Chicago."
    nf_chunk = Chunk(
        id=nf_chunk_id,
        document_id=DOC_ID,
        source_format="pdf",
        text=nf_text,
        element_type="paragraph",
        page_number=1,
        bounding_box={"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0, "page_number": 1},
        location_reference="Page: 1",
        workspace_id=WS_ID,
        version=SNAPSHOT_VERSION,
        document_version=SNAPSHOT_VERSION,
        metadata={"document_filename": SOURCE_FILENAME},
    )
    nf_ev = evidence_item_from_chunk(nf_chunk, workspace_id=WS_ID, strategy="bm25", boundary_repo=repo)

    nf_query = "Where and when was the annual shareholder meeting held?"
    nf_binding = bind_financial_query(nf_query)
    assert nf_binding.is_active is False

    nf_ans = "The annual shareholder meeting was held on October 15, 2027 in Chicago."
    nf_payload = json.dumps({"answer": nf_ans, "citations": [nf_chunk_id]})

    nf_state = {
        "query": nf_query,
        "workspace_id": WS_ID,
        "boundary_repo": repo,
        "compressed_text": f"[{nf_chunk_id}]\n{nf_text}",
        "top_chunks": [nf_chunk],
        "retained_evidence_items": [nf_ev],
        "financial_binding": nf_binding,
        "retrieval_candidates": {"compressed_context": [nf_chunk_id]},
        "synthesis_calls": 0,
    }

    with patch("services.llm.llm_service.generate_completion", return_value=(nf_payload, {"input_tokens": 50, "output_tokens": 15})):
        out_nf = run_llm(dict(nf_state))
        gate_nf = {**nf_state, **out_nf}
        gate_nf.update(delivery_gate(gate_nf))

    assert gate_nf["final_answer"] == nf_ans
    assert len(gate_nf["citations"]) == 1

    eval_nf = evaluate_grounded_answer(
        answer=gate_nf["final_answer"],
        citations=gate_nf["citations"],
        is_answer_text_correct=True,
        contract_type="semantic",
        contract={"contract_type": "semantic", "expected_answer": "October 15, 2027 in Chicago"},
        expected_sources=[SOURCE_FILENAME],
        expected_source_version=SNAPSHOT_VERSION,
        retained_evidence_items=[nf_ev],
        context_chunk_ids=[nf_chunk_id],
        ground_truth_chunk_ids=[nf_chunk_id],
        query=nf_query,
    )
    assert eval_nf["is_grounded_correct"] is True
    assert eval_nf["reason"] == "grounded_and_verified"


def test_live_synthesis_prompt_excludes_benchmark_data():
    """Verify that the actual system prompt sent to the LLM provider excludes all benchmark query text, answer values, and chunk locators."""
    from services.llm.llm_service import call as llm_service_call

    captured_system_prompt = None

    def fake_generate_completion(system_prompt, user_prompt, **kwargs):
        nonlocal captured_system_prompt
        captured_system_prompt = system_prompt
        return json.dumps({"answer": "Acme ROA is 12.5%.", "citations": []}), {"input_tokens": 50, "output_tokens": 10}

    with patch("services.llm.llm_service.generate_completion", side_effect=fake_generate_completion):
        llm_service_call(
            query="What was the return on assets for Acme Corp?",
            compressed_context="Acme Corp reported ROA of 12.5%.",
        )

    assert captured_system_prompt is not None

    # 1. Benchmark-derived arithmetic & specific values must NOT be present
    benchmark_forbidden_values = [
        "30,739",
        "109,417",
        "28.09%",
        "91.92%",
        "61.25%",
        "30.67",
        "2,624",
        "882",
        "874",
        "-92.55",
        "17 December 2014",
        "4 May 2016",
    ]
    for val in benchmark_forbidden_values:
        assert val not in captured_system_prompt, f"Benchmark value '{val}' leaked into live synthesis prompt!"

    # 2. Benchmark source filenames and synthetic document IDs must NOT be present
    benchmark_forbidden_locators = [
        "SEC-Form-10Q.pdf",
        "2212.14776v3.pdf",
        "2412.20875v1.pdf",
        "2501.05730v1.pdf",
        "2501.09166v1.pdf",
        "NFHS_5_India_Districts_Factsheet_Data.xls",
        "survay.csv",
        "rs_status_bill_passed_assent-1952-2016.csv",
        "National-Strategy-for-Artificial-Intelligence.pdf",
        "Doc-fcc723b5",
        "Doc-34202080",
    ]
    for loc in benchmark_forbidden_locators:
        assert loc not in captured_system_prompt, f"Benchmark locator '{loc}' leaked into live synthesis prompt!"

    # 3. Canonical test questions must NOT be present
    test_json_path = Path(__file__).resolve().parent.parent.parent / "data" / "test.json"
    if test_json_path.exists():
        with open(test_json_path) as f:
            test_data = json.load(f)
        for q in test_data.get("questions", []):
            q_text = q.get("question", "").strip()
            if q_text:
                assert q_text.lower() not in captured_system_prompt.lower(), f"Benchmark question '{q_text}' leaked into live synthesis prompt!"


def test_workbook_discovery_for_sheet_queries_and_isolation(monkeypatch):
    """Verify workbook discovery brings eligible Sheet1 rows into pre_rerank_chunk_ids while enforcing workspace, version, and document scope isolation."""
    from unittest.mock import MagicMock, patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.langgraph_pipeline import run_bm25, run_reranker

    ws_main = "ws_wb_main"
    ws_other = "ws_wb_other"
    doc_wb = "doc_health_wb"
    doc_wb_other = "doc_finance_wb"
    doc_pdf1 = "doc_pdf_distractor_1"
    doc_pdf2 = "doc_pdf_distractor_2"

    # Active workbook chunk (Sheet1 row lacking 'Sheet2', 'Sheet3', 'workbook')
    wb_active = Chunk(
        id=f"{doc_wb}:xls:Sheet1:row1",
        document_id=doc_wb,
        text="District: Northville | Population: 120,000 | Clinics: 14",
        page_number=1,
        element_type="table",
        source_format="xls",
        workspace_id=ws_main,
        metadata={"document_filename": "Health_Districts_Data.xls", "document_version": "v2", "sheet_name": "Sheet1"},
    )
    # Stale version chunk of same workbook
    wb_stale = Chunk(
        id=f"{doc_wb}:xls:Sheet1:row1:v1",
        document_id=doc_wb,
        text="District: Oldville | Population: 90,000",
        page_number=1,
        element_type="table",
        source_format="xls",
        workspace_id=ws_main,
        metadata={"document_filename": "Health_Districts_Data.xls", "document_version": "v1", "sheet_name": "Sheet1"},
    )
    # Out-of-scope workbook in same workspace
    wb_out_of_scope = Chunk(
        id=f"{doc_wb_other}:xls:Sheet1:row1",
        document_id=doc_wb_other,
        text="District: Southville | Budget: 500,000",
        page_number=1,
        element_type="table",
        source_format="xls",
        workspace_id=ws_main,
        metadata={"document_filename": "Finance_Districts_Data.xls", "document_version": "v1", "sheet_name": "Sheet1"},
    )
    # Other workspace workbook
    wb_other_ws = Chunk(
        id=f"doc_other_ws:xls:Sheet1:row1",
        document_id="doc_other_ws",
        text="District: Eastville | Population: 50,000",
        page_number=1,
        element_type="table",
        source_format="xls",
        workspace_id=ws_other,
        metadata={"document_filename": "Health_Districts_Data.xls", "document_version": "v2", "sheet_name": "Sheet1"},
    )
    # Distractor PDFs mentioning Sheet2, Sheet3, and workbook
    pdf1 = Chunk(
        id=f"{doc_pdf1}:page:1:element:1",
        document_id=doc_pdf1,
        text="The workbook guide explains Sheet2 and Sheet3 formatting conventions for annual reports.",
        page_number=1,
        element_type="narrative",
        source_format="pdf",
        workspace_id=ws_main,
        metadata={"document_filename": "Reporting_Guide.pdf", "document_version": "1"},
    )
    pdf2 = Chunk(
        id=f"{doc_pdf2}:page:2:element:1",
        document_id=doc_pdf2,
        text="Sheet2 and Sheet3 in a standard workbook may contain district summaries.",
        page_number=2,
        element_type="narrative",
        source_format="pdf",
        workspace_id=ws_main,
        metadata={"document_filename": "Data_Manual.pdf", "document_version": "1"},
    )

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_main,
            document_id=doc_wb,
            source="Health_Districts_Data.xls",
            version="v2",
            format="xls",
            incomplete_coverage_flag=False,
            persisted_rows=1,
            total_rows=1,
            created_at="2026-11-25T00:00:00Z",
        )
    )

    ws_chunks = {
        ws_main: [pdf1, pdf2, wb_active, wb_stale, wb_out_of_scope],
        ws_other: [wb_other_ws],
    }
    mock_repo = MagicMock()
    mock_repo.get_all_chunks.side_effect = lambda document_ids=None, workspace_id="": ws_chunks.get(workspace_id, [])

    monkeypatch.setattr("services.langgraph_pipeline._get_repo", lambda: mock_repo)
    monkeypatch.setattr("services.retrieval.bm25_retriever.get_cached_chunks", lambda ws, fn: fn())

    # 1. Eligible workbook reaches pre_rerank_chunk_ids; stale/other-ws/out-of-scope excluded
    query_wb = "Are Sheet2 and Sheet3 additional populated district datasets in the Health workbook?"
    bm25_out = run_bm25({
        "query": query_wb,
        "workspace_id": ws_main,
        "document_ids": [doc_wb, doc_pdf1, doc_pdf2],
        "boundary_repo": boundary_repo,
        "stage_timings": {},
    })
    with patch("services.langgraph_pipeline.rerank", return_value=(bm25_out["bm25_candidates"][:2], "lexical_bm25_plus", {})):
        rr_out = run_reranker({
            "query": query_wb,
            "workspace_id": ws_main,
            "bm25_candidates": bm25_out["bm25_candidates"],
            "candidate_chunks": [],
            "graph_chunks": [],
            "structural_candidates": [],
            "okf_candidates": [],
            "stage_timings": {},
            "retrieval_candidates": {},
        })
    pre_ids = rr_out["retrieval_candidates"]["pre_rerank"]
    assert wb_active.id in pre_ids
    assert wb_stale.id not in pre_ids
    assert wb_out_of_scope.id not in pre_ids
    assert wb_other_ws.id not in pre_ids

    # 2. Generic-only query without identifying workbook token must NOT select ambiguous workbook
    from services.langgraph_pipeline import _discover_workbook_candidates
    assert _discover_workbook_candidates("Are Sheet2 and Sheet3 populated in the workbook?", [wb_active, wb_out_of_scope], set()) == []

    # 3. PDF-only control adds zero workbook candidates
    ws_chunks["ws_pdf_only"] = [pdf1, pdf2]
    bm25_pdf_only = run_bm25({
        "query": query_wb,
        "workspace_id": "ws_pdf_only",
        "boundary_repo": boundary_repo,
        "stage_timings": {},
    })
    assert all(c.source_format == "pdf" for c in bm25_pdf_only["bm25_candidates"])


def test_legacy_workbook_discovery_via_registered_snapshot_without_chunk_filename(monkeypatch):
    """Reproduce live Q150 miss: legacy XLS chunk has NO document_filename/version metadata, registered DocumentSnapshot supplies source, and 45 competing PDFs push it out of BM25 top-40."""
    from unittest.mock import MagicMock, patch
    from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
    from schemas.contracts.boundary import DocumentSnapshot
    from schemas.models import Chunk
    from services.langgraph_pipeline import run_bm25, run_reranker

    ws_id = "ws_legacy_wb"
    doc_wb = "doc_legacy_health_wb"

    # Legacy XLS chunk matching xls_adapter.py output: NO document_filename, NO document_version
    wb_legacy = Chunk(
        id=f"{doc_wb}:xls:Sheet1:row1",
        document_id=doc_wb,
        text="District Names: Northville. Population: 120000.",
        page_number=1,
        element_type="row",
        source_format="xls",
        workspace_id=ws_id,
        metadata={"sheet": "Sheet1", "row": 2},
    )

    # 45 competing PDFs mentioning Sheet2, Sheet3, and workbook so BM25 top-40 is saturated by PDFs
    pdf_chunks = [
        Chunk(
            id=f"doc_pdf_{i}:page:1:element:1",
            document_id=f"doc_pdf_{i}",
            text=f"Report {i}: Sheet2 and Sheet3 in the workbook contain district annual summary notes.",
            page_number=1,
            element_type="narrative",
            source_format="pdf",
            workspace_id=ws_id,
            metadata={"document_filename": f"Guide_{i}.pdf", "document_version": "1"},
        )
        for i in range(45)
    ]

    boundary_repo = InMemoryWorkspaceBoundaryRepository()
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_wb,
            source="Health_Districts_Data.xls",
            version="v_active",
            format="xls",
            incomplete_coverage_flag=False,
            persisted_rows=1,
            total_rows=1,
            created_at="2026-11-25T00:00:00Z",
        )
    )

    mock_repo = MagicMock()
    mock_repo.get_all_chunks.return_value = pdf_chunks + [wb_legacy]
    monkeypatch.setattr("services.langgraph_pipeline._get_repo", lambda: mock_repo)
    monkeypatch.setattr("db.workspace_boundary_repo.get_workspace_boundary_repo", lambda: boundary_repo)
    monkeypatch.setattr("services.retrieval.bm25_retriever.get_cached_chunks", lambda ws, fn: fn())

    query_wb = "Are Sheet2 and Sheet3 additional populated district datasets in the Health workbook?"
    bm25_out = run_bm25({
        "query": query_wb,
        "workspace_id": ws_id,
        "stage_timings": {},
    })
    with patch("services.langgraph_pipeline.rerank", return_value=(bm25_out["bm25_candidates"][:6], "lexical_bm25_plus", {})):
        rr_out = run_reranker({
            "query": query_wb,
            "workspace_id": ws_id,
            "bm25_candidates": bm25_out["bm25_candidates"],
            "candidate_chunks": [],
            "graph_chunks": [],
            "structural_candidates": [],
            "okf_candidates": [],
            "stage_timings": {},
            "retrieval_candidates": {},
        })

    pre_ids = rr_out["retrieval_candidates"]["pre_rerank"]
    assert wb_legacy.id in pre_ids
    # Never infer/mutate chunk version from the snapshot
    assert wb_legacy.document_version is None
    assert "document_version" not in wb_legacy.metadata



