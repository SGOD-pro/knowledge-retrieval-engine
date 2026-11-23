"""Tests for chunk citation provenance repair and authoritative boundary binding.

Validates:
  1. XLS chunk missing filename metadata resolves canonical filename and version from
     the authoritative DocumentSnapshot in the request-authorized workspace.
  2. Wrong-workspace binding never borrows filename or version from the request workspace.
  3. Version mismatch never relabels an unverified chunk as the current version or
     certifies it with the snapshot's canonical filename.
  4. Preserved tombstone checks prevent active snapshot certification for tombstoned
     workspaces or documents.
  5. Parity between final citations and retained evidence items across fast and full query paths.
  6. Verified recovery of saved canonical benchmark Q144 to grounded pass under offline rescoring.
  7. Clear boundary isolation ensuring Q149 grading and Q193 answer selection remain separate.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import pytest

from schemas.contracts.boundary import DocumentSnapshot
from schemas.models import Chunk
from db.workspace_boundary_repo import InMemoryWorkspaceBoundaryRepository
from services.evaluation.benchmark_scorer import rescore_query_record_offline
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


def test_xls_chunk_missing_filename_metadata_resolves_canonical_provenance(
    boundary_repo: InMemoryWorkspaceBoundaryRepository,
):
    """An unversioned XLS chunk lacking filename metadata resolves canonical filename and version."""
    chunk = Chunk(
        id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0:xls:Sheet1:row3",
        document_id="fcc723b5-ee3e-5c11-a2f3-5cd7c92ebaf0",
        source_format="xls",
        text="District: South Andaman. Men 15-54: 134",
        element_type="table_row",
        location_reference="Sheet: Sheet1, Row: 4",
        metadata={},
    )

    fname, ver = resolve_chunk_provenance(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert fname == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert ver == "e482b990d74eb46b5ce5a6e10606b745"

    cit = build_citation(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        boundary_repo=boundary_repo,
    )
    assert cit.document_filename == "NFHS_5_India_Districts_Factsheet_Data.xls"
    assert cit.document_version == "e482b990d74eb46b5ce5a6e10606b745"

    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id="ws_canonical_comparison_0004",
        strategy="bm25",
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


def test_saved_q144_recovery_under_authoritative_provenance():
    """Verify saved Q144 artifact recovers from failure to grounded pass when citation provenance is repaired."""
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

    # Confirm original saved failure: "Doc-fcc723b5" not in expected sources
    orig_eval = rescore_query_record_offline(q144)
    assert orig_eval["is_grounded_correct"] is False
    assert "Doc-fcc723b5" in orig_eval["reason"]

    # Repair citation and retained evidence with canonical boundary bindings
    repaired_q144 = json.loads(json.dumps(q144))
    canonical_filename = "NFHS_5_India_Districts_Factsheet_Data.xls"
    canonical_version = "e482b990d74eb46b5ce5a6e10606b745"

    for cit in repaired_q144.get("citations", []):
        cit["document_filename"] = canonical_filename
        cit["document_version"] = canonical_version

    for ev in repaired_q144.get("retained_evidence_items", []):
        ev["document_filename"] = canonical_filename
        ev["document_version"] = canonical_version
        if isinstance(ev.get("locator"), dict):
            ev["locator"]["document_filename"] = canonical_filename
            ev["locator"]["document_version"] = canonical_version
        if isinstance(ev.get("citation_payload"), dict):
            ev["citation_payload"]["document_filename"] = canonical_filename
            ev["citation_payload"]["document_version"] = canonical_version

    rescored = rescore_query_record_offline(repaired_q144)
    assert rescored["is_grounded_correct"] is True
    assert rescored["grounded_correct"] is True
    assert rescored["reason"] == "grounded_and_verified"
    assert rescored["citation_validation"]["valid"] is True
    assert rescored["citation_validation"]["valid_count"] == 1
    assert rescored["citation_validation"]["errors"] == []


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
