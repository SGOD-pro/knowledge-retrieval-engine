"""Offline regressions for Phase 3 shared failure groups (malformed JSON recovery, deterministic_math dedup)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = BACKEND_ROOT / "src"
for p in (str(BACKEND_ROOT), str(SRC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from schemas.models import Chunk
from services.langgraph_pipeline import run_deterministic_math, run_llm
from services.llm.llm_service import call as llm_call
from src.schemas.contracts.boundary import DocumentSnapshot


def test_malformed_json_with_extra_bracket_recovers_answer_and_citations():
    """Reproduce Q191-class failure: trailing ']]}' must not leak raw JSON or dump all 6 top_chunks."""
    bad_json = (
        '{"answer": "Attention models can be accurate yet non-interpretable.", '
        '"citations": ["doc-a:page:7:element:6"]]}'
    )
    with patch("services.llm.llm_service.generate_completion", return_value=(bad_json, {"input_tokens": 10, "output_tokens": 10})):
        res = llm_call("How does accuracy relate to interpretability?", "[doc-a:page:7:element:6]\nSome text.")

    assert not res["answer"].lstrip().startswith("{")
    assert res["answer"] == "Attention models can be accurate yet non-interpretable."
    assert res["citations"] == ["doc-a:page:7:element:6"]

    # Control: clean JSON continues to parse normally
    good_json = '{"answer": "Valid answer.", "citations": ["doc-a:page:7:element:6"]}'
    with patch("services.llm.llm_service.generate_completion", return_value=(good_json, {"input_tokens": 10, "output_tokens": 10})):
        res_good = llm_call("Query", "[doc-a:page:7:element:6]\nSome text.")
    assert res_good["answer"] == "Valid answer."
    assert res_good["citations"] == ["doc-a:page:7:element:6"]

    # End-to-end run_llm check: distractor doc-b chunk in top_chunks must NOT be cited
    c_a = Chunk(id="doc-a:page:7:element:6", document_id="doc-a", source_format="pdf", text="FCAM accuracy 100%.", element_type="text", page_number=7, workspace_id="ws_1")
    c_b = Chunk(id="doc-b:page:10:element:9", document_id="doc-b", source_format="pdf", text="Unrelated distractor.", element_type="text", page_number=10, workspace_id="ws_1")
    state = {
        "query": "Explain FCAM",
        "workspace_id": "ws_1",
        "compressed_text": "[doc-a:page:7:element:6]\nFCAM accuracy 100%.\n[doc-b:page:10:element:9]\nUnrelated distractor.",
        "top_chunks": [c_a, c_b],
        "retrieval_candidates": {"compressed_context": [c_a.id, c_b.id]},
        "synthesis_calls": 0,
    }
    with patch("services.llm.llm_service.generate_completion", return_value=(bad_json, {"input_tokens": 10, "output_tokens": 10})):
        out = run_llm(state)
    assert [c["chunk_id"] for c in out["citations"]] == ["doc-a:page:7:element:6"]


def test_deterministic_math_deduplicates_identical_provenance_citations(monkeypatch):
    """Reproduce Q138/Q139/Q161 duplicate citation IDs from multiple operands on the same chunk."""
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "1")
    ws_id = "ws_math"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Math", "owner-1", workspace_id=ws_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc-10q",
            source="SEC-Form-10Q.pdf",
            version="v1",
            format="pdf",
            created_at="2026-11-26T00:00:00Z",
        )
    )
    c1 = Chunk(
        id="doc-10q:page:4:element:3",
        document_id="doc-10q",
        source_format="pdf",
        text="Services 30,739. Total net sales 109,417.",
        element_type="table",
        page_number=4,
        workspace_id=ws_id,
        version="v1",
        document_version="v1",
    )

    class _FakeExecResult:
        overall_confidence = 0.95
        provenance_citations = [{"chunk_id": c1.id}, {"chunk_id": c1.id}]

        def format_answer(self, q):
            return "28.09%, calculated as 30739 / 109417 × 100"

    with patch("services.retrieval.deterministic_executor.DeterministicExecutor.resolve_and_execute", return_value=_FakeExecResult()):
        out = run_deterministic_math(
            {
                "query": "What percentage of total net sales came from Services?",
                "workspace_id": ws_id,
                "boundary_repo": b_repo,
                "top_chunks": [c1],
                "force_full_path": False,
            }
        )

    assert [c["chunk_id"] for c in out["citations"]] == [c1.id]
