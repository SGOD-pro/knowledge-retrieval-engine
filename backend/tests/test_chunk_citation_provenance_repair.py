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
import json
from pathlib import Path
import pytest

from schemas.contracts.boundary import DocumentSnapshot
from schemas.models import Chunk
from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
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

    # Confirm ResponseObject delivery also blocks citation certification under active tombstone
    resp_tombstoned = ResponseObject(gate_state)
    assert resp_tombstoned.citations[0]["document_filename"] != "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert resp_tombstoned.citations[0]["document_filename"] == "Doc-fcc723b5"
    assert resp_tombstoned.citations[0]["document_version"] is None
    assert resp_tombstoned.citations[1]["document_filename"] != "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert resp_tombstoned.citations[1]["document_filename"] == "Doc-fcc723b5"
    assert resp_tombstoned.citations[1]["document_version"] is None
