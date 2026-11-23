"""Tests for chunk citation provenance repair and authoritative boundary binding.

Validates:
  1. An unbound chunk resolves canonical filename but stays unversioned even after snapshot advances.
  2. A verified chunk carries matching canonical filename and version through the actual query path.
  3. Wrong-workspace binding never borrows filename or version from the request workspace.
  4. Version mismatch never relabels an unverified chunk as the current version or
     certifies it with the snapshot's canonical filename.
  5. Preserved tombstone checks prevent active snapshot certification for tombstoned
     workspaces or documents.
  6. Parity between final citations and retained evidence items across fast and full query paths.
  7. Scorer diagnostic demonstrating evaluator behavior under simulated canonical provenance
     while preserving historical artifacts intact.
  8. Clear boundary isolation ensuring Q149 grading and Q193 answer selection remain separate.
"""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from db.database import CloudRepository
from db.redis_cache import cache
from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from ingestion.embed_service import embed_fast_local, _deterministic_vector
from modules.query.query_service import QueryService
from schemas.contracts.boundary import DocumentSnapshot
from schemas.models import Chunk, Document, QueryRequest
from services.evaluation.benchmark_scorer import rescore_query_record_offline
from services.langgraph_pipeline import ResponseObject, delivery_gate
from services.retrieval.evidence_contract import EvidenceItem, evidence_item_from_chunk
from services.retrieval.response_builder import (
    Citation,
    build_citation,
    build_fast_path_response,
    resolve_chunk_provenance,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def boundary_repo() -> InMemoryWorkspaceBoundaryRepository:
    repo = InMemoryWorkspaceBoundaryRepository()
    repo.create_workspace("Test Workspace", "owner_1", "ws_canonical_comparison_0004")
    # Add authoritative document snapshot for Q144 NFHS dataset
    repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_canonical_comparison_0004",
            document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
            source="NFHS_5_India_Districts_Factsheet_Data.xls",
            version="e482b990d74eb46b5ce5a6e10606b745",
            format="xls",
            created_at=_now_utc(),
        )
    )
    # Add second snapshot for versioned document
    repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_canonical_comparison_0004",
            document_id="doc_sec_10q",
            source="SEC-Form-10Q.pdf",
            version="ver_2026_q3",
            format="pdf",
            created_at=_now_utc(),
        )
    )
    return repo


def test_unbound_chunk_stays_unversioned_after_snapshot_advances(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """An unbound chunk resolves canonical filename but stays unversioned even after snapshot advances."""
    chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="District: South Andaman. Men 15-54: 134",
        element_type="table_row",
        location_reference="Sheet: Sheet1, Row: 4",
        metadata={},
    )

    # Initial snapshot has version e482b990d74eb46b5ce5a6e10606b745
    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ver is None  # A document snapshot alone cannot supply a version for an unversioned chunk

    cit = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert cit.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert cit.document_version is None

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="bm25",
        boundary_repo=boundary_repo,
    )
    assert ev.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.document_version is None
    assert ev.locator["document_filename"] == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.locator["document_version"] is None
    assert ev.citation_payload["document_filename"] == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.citation_payload["document_version"] is None

    # Advance the document snapshot to a new version in the authorized workspace
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id="ws_canonical_comparison_0004",
            document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
            source="NFHS_5_India_Districts_Factsheet_Data.xls",
            version="e482b990d74eb46b5ce5a6e10606b745_v2_advanced",
            format="xls",
            created_at=_now_utc(),
        )
    )

    # The unbound chunk must still stay unversioned after the snapshot advances
    fname2, ver2 = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname2 == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ver2 is None

    cit2 = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert cit2.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert cit2.document_version is None

    ev2 = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="bm25",
        boundary_repo=boundary_repo,
    )
    assert ev2.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev2.document_version is None
    assert ev2.locator["document_version"] is None
    assert ev2.citation_payload["document_version"] is None


def test_verified_chunk_carries_matching_provenance_through_actual_query_path(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """A verified chunk carries matching canonical filename and version through the actual query path."""
    chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="District: South Andaman. Men 15-54: 134",
        element_type="table_row",
        location_reference="Sheet: Sheet1, Row: 4",
        document_version="e482b990d74eb46b5ce5a6e10606b745",
        metadata={"version": "e482b990d74eb46b5ce5a6e10606b745"},
    )

    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ver == "e482b990d74eb46b5ce5a6e10606b745"

    response = build_fast_path_response(
        query="How many men in South Andaman?",
        scored_chunks=[(chunk, 0.95)],
        latency_breakdown={"total_ms": 10.0},
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert len(response.citations) == 1
    cit = response.citations[0]
    assert cit.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert cit.document_version == "e482b990d74eb46b5ce5a6e10606b745"

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="vector_rerank",
        boundary_repo=boundary_repo,
    )
    assert ev.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.document_version == "e482b990d74eb46b5ce5a6e10606b745"
    assert ev.locator["document_filename"] == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.locator["document_version"] == "e482b990d74eb46b5ce5a6e10606b745"
    assert ev.citation_payload["document_filename"] == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev.citation_payload["document_version"] == "e482b990d74eb46b5ce5a6e10606b745"


def test_wrong_workspace_binding_never_borrows_metadata(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """A chunk with a foreign workspace binding must never borrow filename or version."""
    chunk = Chunk(
        id="chunk_foreign_1",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="Foreign workspace data",
        element_type="text",
        workspace_id="ws_foreign_workspace",
        version="v_foreign",
        metadata={"workspace_id": "ws_foreign_workspace"},
    )

    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    # Must NOT borrow NFHS filename or canonical version from ws_canonical_comparison_0004
    assert fname != "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert fname == "Doc-fcc723b5"
    assert ver == "v_foreign"

    cit = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert cit.document_filename == "Doc-fcc723b5"
    assert cit.document_version == "v_foreign"

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="vector_rerank",
        boundary_repo=boundary_repo,
    )
    assert ev.document_filename == "Doc-fcc723b5"
    assert ev.document_version == "v_foreign"


def test_version_mismatch_never_relabels_unverified_chunk(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """A chunk asserting an unverified version must never be relabeled as the snapshot version."""
    chunk = Chunk(
        id="chunk_stale_ver_1",
        document_id="doc_sec_10q",
        source_format="pdf",
        text="Stale Apple filing paragraph",
        element_type="text",
        document_version="ver_2025_q1",  # snapshot is ver_2026_q3
        metadata={"version": "ver_2025_q1"},
    )

    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    # Must NOT certify with snapshot source or relabel as current version
    assert fname != "SEC-Form-10Q.pdf"
    assert fname == "Doc-doc_sec_"
    assert ver == "ver_2025_q1"

    cit = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert cit.document_filename == "Doc-doc_sec_"
    assert cit.document_version == "ver_2025_q1"

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="vector_rerank",
        boundary_repo=boundary_repo,
    )
    assert ev.document_filename == "Doc-doc_sec_"
    assert ev.document_version == "ver_2025_q1"


def test_tombstone_checks_prevent_active_snapshot_certification(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """Tombstoned workspaces and sources must never certify active snapshots."""
    # 1. Source-level tombstone
    boundary_repo.record_source_tombstone(
        workspace_id="ws_canonical_comparison_0004",
        source_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        caller_principal_id="admin_1",
        reason="document_revoked",
    )

    chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="Data from revoked document",
        element_type="table_row",
    )

    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname != "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert fname == "Doc-fcc723b5"

    # 2. Workspace-level tombstone
    boundary_repo.tombstone_workspace(
        workspace_id="ws_canonical_comparison_0004",
        caller_principal_id="owner_1",
        reason="workspace_deleted",
    )

    chunk2 = Chunk(
        id="chunk_sec_1",
        document_id="doc_sec_10q",
        source_format="pdf",
        text="Data from deleted workspace",
        element_type="text",
    )

    fname2, ver2 = resolve_chunk_provenance(
        chunk=chunk2,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname2 != "SEC-Form-10Q.pdf"
    assert fname2 == "Doc-doc_sec_"


def test_fast_query_path_citation_and_evidence_parity(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """Fast query path construction preserves identical filename and version parity."""
    chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="South Andaman 134 men",
        element_type="table_row",
        location_reference="Sheet: Sheet1, Row: 4",
        document_version="e482b990d74eb46b5ce5a6e10606b745",
        metadata={"version": "e482b990d74eb46b5ce5a6e10606b745"},
    )

    response = build_fast_path_response(
        query="How many men in South Andaman?",
        scored_chunks=[(chunk, 0.95)],
        latency_breakdown={"total_ms": 12.0},
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="vector_rerank",
        boundary_repo=boundary_repo,
    )

    assert len(response.citations) == 1
    cit = response.citations[0]
    assert cit.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert cit.document_version == "e482b990d74eb46b5ce5a6e10606b745"
    assert ev.document_filename == cit.document_filename
    assert ev.document_version == cit.document_version


def test_full_query_path_citation_and_evidence_parity(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """Full path synthesis preserves identical filename and version parity."""
    chunk = Chunk(
        id="doc_sec_10q:page:1:element:0",
        document_id="doc_sec_10q",
        source_format="pdf",
        text="Apple Commission File Number 001-36743",
        element_type="text",
        page_number=1,
        location_reference="Page: 1",
        document_version="ver_2026_q3",
        metadata={"version": "ver_2026_q3"},
    )

    cit = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="vector_rerank",
        boundary_repo=boundary_repo,
    )

    assert cit.document_filename == "SEC-Form-10Q.pdf"
    assert cit.document_version == "ver_2026_q3"
    assert ev.document_filename == cit.document_filename
    assert ev.document_version == cit.document_version


def test_q144_manual_rewrite_scorer_diagnostic():
    """Demonstrate offline scorer behavior as a diagnostic under simulated canonical provenance.

    Important boundary:
      This in-memory rewrite test is strictly an offline scorer diagnostic showing that
      the grounded-answer evaluator passes when citations and retained evidence carry
      the canonical filename ('NFHS_5_India_Districts_Factsheet_Data.xls') and version.
      The untouched historical artifact in query_details.json remains unmodified and
      continues to fail rescoring because historical benchmark artifacts are preserved intact.
      Benchmark recovery cannot be claimed from a manual JSON rewrite; recovery occurs
      only when the actual query path produces verified chunk provenance.
    """
    report_path = (
        Path(__file__).resolve().parent
        / ".."
        / "reports"
        / "73075af21d9172155cad22866d8eef2e7c186edb"
        / "20261123_075022"
        / "query_details.json"
    )
    if not report_path.exists():
        pytest.skip(f"Historical run report not found at {report_path}")

    with open(report_path, encoding="utf-8") as f:
        records = json.load(f)

    q144 = next((r for r in records if r.get("id") == "Q144"), None)
    assert q144 is not None

    # Confirm original saved failure: "Doc-fcc723b5" not in expected sources.
    # The historical record is preserved intact without mutation.
    orig_eval = rescore_query_record_offline(q144)
    assert orig_eval["is_grounded_correct"] is False
    assert "Doc-fcc723b5" in orig_eval["reason"]

    # Scorer Diagnostic: simulate authoritative boundary bindings in a memory-only copy
    # to evaluate scorer behavior when canonical provenance is provided.
    diagnostic_q144 = json.loads(json.dumps(q144))
    canonical_filename = "NFHS_5_India_Districts_Factsheet_Data.xls"
    canonical_version = "e482b990d74eb46b5ce5a6e10606b745"

    for cit in diagnostic_q144.get("citations", []):
        cit["document_filename"] = canonical_filename
        cit["document_version"] = canonical_version

    for ev in diagnostic_q144.get("retained_evidence_items", []):
        ev["document_filename"] = canonical_filename
        ev["document_version"] = canonical_version
        if isinstance(ev.get("locator"), dict):
            ev["locator"]["document_filename"] = canonical_filename
            ev["locator"]["document_version"] = canonical_version
        if isinstance(ev.get("citation_payload"), dict):
            ev["citation_payload"]["document_filename"] = canonical_filename
            ev["citation_payload"]["document_version"] = canonical_version

    # The scorer accepts canonical provenance, confirming the diagnostic hypothesis.
    # Note: this proves scorer rules are sound; actual benchmark recovery requires
    # the live query path to produce verified provenance.
    diagnostic_eval = rescore_query_record_offline(diagnostic_q144)
    assert diagnostic_eval["is_grounded_correct"] is True
    assert diagnostic_eval["grounded_correct"] is True
    assert diagnostic_eval["reason"] == "grounded_and_verified"
    assert diagnostic_eval["citation_validation"]["valid"] is True
    assert diagnostic_eval["citation_validation"]["valid_count"] == 1
    assert diagnostic_eval["citation_validation"]["errors"] == []


def test_q149_and_q193_remain_isolated_and_unaltered():
    """Verify saved Q149 and Q193 maintain their distinct failure modes without modification."""
    report_path = (
        Path(__file__).resolve().parent
        / ".."
        / "reports"
        / "73075af21d9172155cad22866d8eef2e7c186edb"
        / "20261123_075022"
        / "query_details.json"
    )
    if not report_path.exists():
        pytest.skip(f"Historical run report not found at {report_path}")

    with open(report_path, encoding="utf-8") as f:
        records = json.load(f)

    q149 = next((r for r in records if r.get("id") == "Q149"), None)
    assert q149 is not None
    q149_eval = rescore_query_record_offline(q149)
    # Q149 failed text-grading on 99.58% tokenization
    assert q149_eval["is_grounded_correct"] is False
    assert q149_eval["reason"] == "answer_text_incorrect"

    q193 = next((r for r in records if r.get("id") == "Q193"), None)
    assert q193 is not None
    q193_eval = rescore_query_record_offline(q193)
    # Q193 failed due to wrong selected answer number (23,245 instead of 30,739)
    assert q193_eval["is_grounded_correct"] is False
    assert q193_eval["reason"] == "answer_text_incorrect"


def test_actual_query_path_snapshot_change_and_tombstone_before_delivery(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """Exercise query path with snapshot change between evidence and citation, and tombstone before delivery."""
    v_chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="South Andaman 134 men",
        element_type="table_row",
        document_version="e482b990d74eb46b5ce5a6e10606b745",
        metadata={"version": "e482b990d74eb46b5ce5a6e10606b745"},
    )
    u_chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:row4",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="South Andaman unbound men",
        element_type="table_row",
    )

    ws_id = "ws_canonical_comparison_0004"

    # Step A: Evidence creation during retrieval (snapshot version = e482b990d74eb46b5ce5a6e10606b745)
    ev_v = evidence_item_from_chunk(v_chunk, workspace_id=ws_id, strategy="vector_rerank", boundary_repo=boundary_repo)
    ev_u = evidence_item_from_chunk(u_chunk, workspace_id=ws_id, strategy="bm25", boundary_repo=boundary_repo)

    assert ev_v.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev_v.document_version == "e482b990d74eb46b5ce5a6e10606b745"
    assert ev_u.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ev_u.document_version is None  # unbound chunk stays unversioned

    # Step B: Snapshot changes (advances to V2) between evidence creation and final citation construction
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
            source="NFHS_5_India_Districts_Factsheet_Data.xls",
            version="e482b990d74eb46b5ce5a6e10606b745_v2_advanced",
            format="xls",
            created_at=_now_utc(),
        )
    )

    # Step C: Final citation construction and query path response resolution
    cit_v = build_citation(v_chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    cit_u = build_citation(u_chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()

    state = {
        "workspace_id": ws_id,
        "boundary_repo": boundary_repo,
        "final_answer": "South Andaman data verified",
        "citations": [cit_v, cit_u],
        "retained_evidence_items": [ev_v, ev_u],
    }

    resp = ResponseObject(state)

    # Confirm both outputs use the same verified chunk binding
    assert resp.citations[0]["document_filename"] == ev_v.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert resp.citations[0]["document_version"] == ev_v.document_version == "e482b990d74eb46b5ce5a6e10606b745"

    # Confirm that unbound chunk stays unversioned even after snapshot advanced
    assert resp.citations[1]["document_filename"] == ev_u.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert resp.citations[1]["document_version"] is None
    assert ev_u.document_version is None

    # Step D: Tombstone added before delivery blocks the citation
    boundary_repo.record_source_tombstone(
        workspace_id=ws_id,
        source_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        caller_principal_id="admin_1",
        reason="revoked_before_delivery",
    )

    # Confirm delivery gate blocks the citation / suppresses delivery
    gate_state = {
        "workspace_id": ws_id,
        "boundary_repo": boundary_repo,
        "final_answer": "South Andaman data verified",
        "citations": [cit_v, cit_u],
        "retained_evidence_items": [ev_v, ev_u],
    }
    gate_result = delivery_gate(gate_state)
    assert gate_result["final_answer"] == "NOT_FOUND"
    assert gate_result["citations"] == []
    assert gate_result["retrieval_incomplete"] is True

    # Confirm ResponseObject delivery prevents delivery of answer, citations, and retained evidence under active tombstone
    resp_tombstoned = ResponseObject(gate_state)
    assert resp_tombstoned.answer == "NOT_FOUND"
    assert resp_tombstoned.citations == []
    assert resp_tombstoned.retained_evidence_items == []
    assert resp_tombstoned.top_chunks == []
    assert resp_tombstoned.retrieval_incomplete is True
    assert resp_tombstoned.confidence_score == 0.0


def _setup_synthetic_doc(repo: CloudRepository, workspace_id: str, doc_id: str, text: str, format: str = "pdf"):
    doc = Document(
        id=doc_id,
        filename=f"{doc_id}.{format}",
        source_format=format,
        chunks=(
            Chunk(
                id=f"{doc_id}:c1",
                document_id=doc_id,
                text=text,
                source_format=format,
                page_number=1,
                element_type="paragraph" if format == "pdf" else "table_row",
                section_path=("Main",),
                embedding_fast=embed_fast_local(text),
                embedding_full=_deterministic_vector(text, 1024),
                workspace_id=workspace_id,
                similarity_score=0.95,
            ),
        ),
        workspace_id=workspace_id,
    )
    repo.save(doc)
    return doc


def test_real_query_response_path_tombstone_before_gate(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Exercise real query response path when a tombstone is recorded before the gate."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_tombstone_pre_gate_test"
    doc_id = "doc_pre_gate_1"
    boundary_repo.create_workspace("Pre Gate Test", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="pre_gate_record.pdf",
            version="v_pre_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the secret formula in the pre gate document?"
    _setup_synthetic_doc(repo, ws_id, doc_id, "The secret formula in the pre gate document is XYZ-99.")

    # Record source tombstone before query execution / before delivery gate
    boundary_repo.record_source_tombstone(
        workspace_id=ws_id,
        source_id=doc_id,
        caller_principal_id="admin_1",
        reason="revoked_before_gate",
    )

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=False,
    )

    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        res = qs.execute_query(req)

    # Assert safe refusal, no citations, no retained evidence text, and retrieval_incomplete=True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "XYZ-99" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0
    assert res["faithfulness"] is None
    assert res["citation_utilization_rate"] is None


def test_real_query_response_path_tombstone_after_gate(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Exercise real query response path when a tombstone is recorded after the gate."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_tombstone_post_gate_test"
    doc_id = "doc_post_gate_1"
    boundary_repo.create_workspace("Post Gate Test", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="post_gate_record.pdf",
            version="v_post_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the revenue target in post gate?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The revenue target in post gate is 88 million.")
    chunk = doc.chunks[0]

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=False,
    )

    # Synthetic response representing pipeline output before post-gate revocation
    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="fast_path", boundary_repo=boundary_repo)

    mock_resp = ResponseObject({
        "workspace_id": ws_id,
        "boundary_repo": boundary_repo,
        "final_answer": "The revenue target is 88 million.",
        "citations": [cit],
        "retained_evidence_items": [ev],
        "top_chunks": [chunk],
        "confidence_score": 0.95,
        "executed_path": "fast",
        "planned_path": "fast",
        "retrieval_incomplete": False,
    })

    def run_pipeline_and_record_tombstone(**kwargs):
        # Tombstone recorded right after gate execution before QueryService final boundary check
        boundary_repo.record_source_tombstone(
            workspace_id=ws_id,
            source_id=doc_id,
            caller_principal_id="admin_1",
            reason="revoked_after_gate",
        )
        return mock_resp

    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("services.langgraph_pipeline.pipeline.run", side_effect=run_pipeline_and_record_tombstone):
        res = qs.execute_query(req)

    # Assert safe refusal, no citations, no retained evidence text, and retrieval_incomplete=True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "88 million" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0
    assert res["cached"] is False


def test_real_query_response_path_boundary_lookup_error(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Exercise real query response path when a boundary check encounters a lookup error."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_lookup_err_test"
    doc_id = "doc_err_1"
    boundary_repo.create_workspace("Error Test", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="err_record.pdf",
            version="v_err_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the secret data under lookup failure?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The secret data under lookup failure is CLASSIFIED-999.")
    chunk = doc.chunks[0]

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=False,
    )

    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="fast_path", boundary_repo=boundary_repo)

    mock_resp = ResponseObject({
        "workspace_id": ws_id,
        "boundary_repo": boundary_repo,
        "final_answer": "The secret data is CLASSIFIED-999.",
        "citations": [cit],
        "retained_evidence_items": [ev],
        "top_chunks": [chunk],
        "confidence_score": 0.95,
        "executed_path": "fast",
        "planned_path": "fast",
        "retrieval_incomplete": False,
    })

    # Simulate storage lookup failure on boundary check
    def error_has_tombstone(*args, **kwargs):
        raise RuntimeError("Boundary storage connection failure")

    boundary_repo.has_tombstone = error_has_tombstone

    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("services.langgraph_pipeline.pipeline.run", return_value=mock_resp):
        res = qs.execute_query(req)

    # Boundary check must fail closed: safe refusal, no citations, no retained evidence text, retrieval_incomplete=True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "CLASSIFIED-999" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0
    assert res["cached"] is False


def _compute_exact_cache_key(query: str, doc_ids: list[str]) -> str:
    query_norm = query.strip().lower()
    doc_scope_hash = ""
    if doc_ids:
        sorted_ids = sorted(doc_ids)
        doc_scope_hash = hashlib.sha256(",".join(sorted_ids).encode("utf-8")).hexdigest()
    raw_key = (query_norm + doc_scope_hash).encode("utf-8")
    return f"query:{hashlib.sha256(raw_key).hexdigest()}"


def test_exact_cache_hit_revoked_source_after_scope_resolution(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Confirm exact cache hit enforces safe refusal when source is revoked after scope resolution."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_exact_cache_revocation"
    doc_id = "doc_exact_revoked_1"
    boundary_repo.create_workspace("Exact Cache Revocation", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="exact_record.pdf",
            version="v_exact_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the exact cached revenue figure?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The exact cached revenue figure is 99 million.")
    chunk = doc.chunks[0]

    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="fast_path", boundary_repo=boundary_repo)

    cached_payload = {
        "answer": "The exact cached revenue figure is 99 million.",
        "citations": [cit],
        "retained_evidence_items": [ev.to_dict()],
        "confidence": 0.95,
        "confidence_score": 0.95,
        "latency_ms": 10.0,
        "fast_path": True,
        "retrieval_path": "fast",
        "status": "success",
        "retrieval_incomplete": False,
        "cached": False,
    }

    cache_key = _compute_exact_cache_key(query_text, [doc_id])
    cache.set_cache(cache_key, cached_payload, ttl=3600)

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=True,
    )

    real_get_cache = cache.get_cache

    def get_cache_and_revoke(key):
        # Revoke source after scope resolution has already evaluated target_doc_ids
        boundary_repo.record_source_tombstone(
            workspace_id=ws_id,
            source_id=doc_id,
            caller_principal_id="admin_1",
            reason="revoked_after_scope_resolution",
        )
        return real_get_cache(key)

    with patch.object(cache, "get_cache", side_effect=get_cache_and_revoke), \
         patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        res = qs.execute_query(req)

    # Must return safe refusal with no citations or evidence and retrieval_incomplete=True
    assert res["cached"] is True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "99 million" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0


def test_semantic_cache_hit_revoked_source_after_scope_resolution(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Confirm semantic cache hit enforces safe refusal when source is revoked after scope resolution."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_semantic_cache_revocation"
    doc_id = "doc_semantic_revoked_1"
    boundary_repo.create_workspace("Semantic Cache Revocation", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="semantic_record.pdf",
            version="v_sem_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the semantic forecast total?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The semantic forecast total is 120 million.")
    chunk = doc.chunks[0]

    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="vector_rerank", boundary_repo=boundary_repo)

    cached_payload = {
        "answer": "The semantic forecast total is 120 million.",
        "citations": [cit],
        "retained_evidence_items": [ev.to_dict()],
        "confidence": 0.92,
        "confidence_score": 0.92,
        "latency_ms": 25.0,
        "fast_path": False,
        "retrieval_path": "full",
        "status": "success",
        "retrieval_incomplete": False,
        "cached": False,
    }

    semantic_key = f"semantic:{ws_id}:forecast"
    cache.set_cache(semantic_key, cached_payload, ttl=3600)

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=True,
        force_full_path=True,
    )

    real_get_cache = cache.get_cache

    def get_cache_and_revoke(key):
        if key == semantic_key:
            # Revoke source after scope resolution
            boundary_repo.record_source_tombstone(
                workspace_id=ws_id,
                source_id=doc_id,
                caller_principal_id="admin_1",
                reason="revoked_after_scope_resolution",
            )
        return real_get_cache(key)

    with patch.object(cache, "get_cache", side_effect=get_cache_and_revoke), \
         patch.object(qs.repo, "check_semantic_cache", return_value=semantic_key), \
         patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        res = qs.execute_query(req)

    # Must return safe refusal with no citations or evidence and retrieval_incomplete=True
    assert res["cached"] is True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "120 million" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0


def test_cached_query_boundary_lookup_error_returns_safe_refusal(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Confirm cached query fails closed with safe refusal when boundary lookup encounters an error."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_cache_lookup_err"
    doc_id = "doc_cache_err_1"
    boundary_repo.create_workspace("Cache Error Test", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="err_cache_record.pdf",
            version="v_ce_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the confidential cache item?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The confidential cache item is TOP-SECRET-42.")
    chunk = doc.chunks[0]

    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="fast_path", boundary_repo=boundary_repo)

    cached_payload = {
        "answer": "The confidential cache item is TOP-SECRET-42.",
        "citations": [cit],
        "retained_evidence_items": [ev.to_dict()],
        "confidence": 0.95,
        "confidence_score": 0.95,
        "latency_ms": 10.0,
        "fast_path": True,
        "retrieval_path": "fast",
        "status": "success",
        "retrieval_incomplete": False,
        "cached": False,
    }

    cache_key = _compute_exact_cache_key(query_text, [doc_id])
    cache.set_cache(cache_key, cached_payload, ttl=3600)

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=True,
    )

    real_has_tombstone = boundary_repo.has_tombstone

    def error_on_cache_check(workspace_id, entity_type, entity_id):
        # Allow scope resolution to pass, but fail on cache hit boundary check
        if entity_type == "workspace" or entity_id == doc_id:
            raise RuntimeError("DynamoDB boundary table outage during cache check")
        return real_has_tombstone(workspace_id, entity_type, entity_id)

    # Hook has_tombstone to simulate lookup failure during cache validation
    def get_cache_and_set_error(key):
        boundary_repo.has_tombstone = error_on_cache_check
        return cache._mem_cache.get(key)

    with patch.object(cache, "get_cache", side_effect=get_cache_and_set_error), \
         patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        res = qs.execute_query(req)

    # Must fail closed with safe refusal, no citations, no evidence, and retrieval_incomplete=True
    assert res["cached"] is True
    assert "couldn't find any relevant" in res["answer"].lower()
    assert "TOP-SECRET-42" not in res["answer"]
    assert res["citations"] == []
    assert res["retained_evidence_items"] == []
    assert res["retrieval_incomplete"] is True
    assert res["confidence"] == 0.0


def test_authorized_cache_hit_delivers_payload(monkeypatch, boundary_repo: InMemoryWorkspaceBoundaryRepository):
    """Confirm authorized cache hit delivers full answer, citations, and evidence with cached=True."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    ws_id = "ws_authorized_cache"
    doc_id = "doc_auth_1"
    boundary_repo.create_workspace("Authorized Cache Test", "owner_1", ws_id)
    boundary_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="authorized_record.pdf",
            version="v_auth_1",
            format="pdf",
            created_at=_now_utc(),
        )
    )

    repo = CloudRepository()
    query_text = "What is the confirmed delivery date?"
    doc = _setup_synthetic_doc(repo, ws_id, doc_id, "The confirmed delivery date is October 15, 2026.")
    chunk = doc.chunks[0]

    cit = build_citation(chunk, workspace_id=ws_id, boundary_repo=boundary_repo).to_dict()
    ev = evidence_item_from_chunk(chunk, workspace_id=ws_id, strategy="fast_path", boundary_repo=boundary_repo)

    cached_payload = {
        "answer": "The confirmed delivery date is October 15, 2026.",
        "citations": [cit],
        "retained_evidence_items": [ev.to_dict()],
        "confidence": 0.98,
        "confidence_score": 0.98,
        "latency_ms": 8.0,
        "fast_path": True,
        "retrieval_path": "fast",
        "status": "success",
        "retrieval_incomplete": False,
        "cached": False,
    }

    cache_key = _compute_exact_cache_key(query_text, [doc_id])
    cache.set_cache(cache_key, cached_payload, ttl=3600)

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query=query_text,
        cache=True,
    )

    with patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo), \
         patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=boundary_repo):
        res = qs.execute_query(req)

    # Must deliver authorized response
    assert res["cached"] is True
    assert "October 15, 2026" in res["answer"]
    assert len(res["citations"]) == 1
    assert res["citations"][0]["document_id"] == doc_id
    assert len(res["retained_evidence_items"]) == 1
    assert res["retrieval_incomplete"] is False
    assert res["confidence"] == 0.98
