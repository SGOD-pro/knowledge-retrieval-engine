"""Offline regressions for canonical benchmark comparison workspace ingestion helper.

Verifies:
1. Historical workspace protection preventing modification of ws_fresh_benchmark.
2. Complete TableStore manifests and single pass reads verifying exact row counts.
3. Invocation and durable persistence of PageIndex summaries and retrievable targets.
4. Accurate preservation of TableStore counts (60,255 and 727 rows) in CSV snapshots.
5. Accurate preservation of parser chunk coverage in non CSV document snapshots.
6. Explicit reporting of unavailable layouts or failed enrichment preventing false readiness.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
import pytest

from scripts.ingest_canonical_comparison_workspace import (
    EXPECTED_COMBINED_CORPUS_HASH,
    ingest_comparison_corpus,
    verify_corpus_files,
)
from schemas.models import Chunk, Document
from schemas.contracts.boundary import DocumentSnapshot
from schemas.contracts.page_index import ElementLocation, PageIndexSummary


@pytest.fixture
def isolated_smoke_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Configure temporary SQLite databases for isolated storage testing."""
    table_db = tmp_path / "test_tablestore.db"
    boundary_db = tmp_path / "test_boundary.db"
    monkeypatch.setenv("KRE_TABLE_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("KRE_WORKSPACE_REPO_BACKEND", "sqlite")
    monkeypatch.setenv("KRE_OKF_REPO_BACKEND", "sqlite")
    monkeypatch.setenv("KRE_TABLE_STORE_SQLITE_PATH", str(table_db))
    monkeypatch.setenv("KRE_WORKSPACE_SQLITE_PATH", str(boundary_db))
    monkeypatch.setenv("KRE_OKF_SQLITE_PATH", str(boundary_db))
    return table_db, boundary_db


def test_corpus_files_exist_and_hash_matches():
    """Verify all nine canonical files are present and produce the expected combined hash."""
    combined_hex, file_hashes, file_paths = verify_corpus_files()
    assert combined_hex == EXPECTED_COMBINED_CORPUS_HASH
    assert len(file_paths) == 9
    assert "survay.csv" in file_paths
    assert "rs_status_bill_passed_assent-1952-2016.csv" in file_paths


def test_historical_workspace_protection():
    """Verify modifying ws_fresh_benchmark raises ValueError without explicit override."""
    with pytest.raises(ValueError) as excinfo:
        ingest_comparison_corpus(workspace_id="ws_fresh_benchmark", smoke_test=True)
    assert "Refusing to modify preserved historical workspace 'ws_fresh_benchmark'" in str(excinfo.value)


def test_smoke_mode_isolated_storage_and_zero_provider_calls(isolated_smoke_env):
    """Verify smoke test mode completes offline with zero model calls and valid TableStore reads."""
    with patch("db.database.CloudRepository") as mock_cloud_repo:
        results = ingest_comparison_corpus(
            workspace_id="ws_test_smoke_isolated",
            smoke_test=True,
        )

        # CloudRepository must not be instantiated or initialized in smoke mode
        assert not mock_cloud_repo.called

        # Tables verification
        assert "survay" in results["tables"]
        assert "rs_status_bill_passed_assent_1952_2016" in results["tables"]

        survay_info = results["tables"]["survay"]
        assert survay_info["persisted_rows"] == 60255
        assert survay_info["complete_read_count"] == 60255
        assert survay_info["manifest_verified"] is True
        assert survay_info["sample_rows_read"] == 5

        rs_info = results["tables"]["rs_status_bill_passed_assent_1952_2016"]
        assert rs_info["persisted_rows"] == 727
        assert rs_info["complete_read_count"] == 727
        assert rs_info["manifest_verified"] is True
        assert rs_info["sample_rows_read"] == 5

        # Snapshots row counts verification
        snapshots = results["snapshots"]
        assert snapshots["survay.csv"]["persisted_rows"] == 60255
        assert snapshots["survay.csv"]["total_rows"] == 60255
        assert snapshots["survay.csv"]["incomplete_coverage_flag"] is False

        assert snapshots["rs_status_bill_passed_assent-1952-2016.csv"]["persisted_rows"] == 727
        assert snapshots["rs_status_bill_passed_assent-1952-2016.csv"]["total_rows"] == 727
        assert snapshots["rs_status_bill_passed_assent-1952-2016.csv"]["incomplete_coverage_flag"] is False

        assert results["readiness"]["tables_verified"] is True
        assert results["readiness"]["table_rows_total"] == 60982


def test_full_ingestion_invokes_and_persists_page_index_and_preserves_csv_rows(isolated_smoke_env):
    """Verify full ingestion invokes PageIndex extraction, verifies targets, and preserves CSV row counts."""
    ws_id = "ws_test_full_ingest_verification"

    def mock_parse_file(fpath: Path, document_id: str, filename: str, workspace_id: str) -> Document:
        suffix = fpath.suffix.lower()
        if suffix == ".pdf":
            chunks = (
                Chunk(
                    id=f"{document_id}_c1",
                    document_id=document_id,
                    source_format="pdf",
                    text="Title of Academic Paper",
                    element_type="title",
                    page_number=1,
                    workspace_id=workspace_id,
                ),
                Chunk(
                    id=f"{document_id}_c2",
                    document_id=document_id,
                    source_format="pdf",
                    text="Table 1: Benchmark Evaluation Metrics",
                    element_type="table",
                    page_number=2,
                    workspace_id=workspace_id,
                ),
                Chunk(
                    id=f"{document_id}_c3",
                    document_id=document_id,
                    source_format="pdf",
                    text="Figure 2: Pipeline Architecture Overview",
                    element_type="figure",
                    page_number=3,
                    workspace_id=workspace_id,
                ),
            )
        elif suffix == ".csv":
            chunks = (
                Chunk(
                    id=f"{document_id}_csv1",
                    document_id=document_id,
                    source_format="csv",
                    text="Row context sample",
                    element_type="table",
                    page_number=1,
                    workspace_id=workspace_id,
                ),
            )
        else:
            chunks = (
                Chunk(
                    id=f"{document_id}_other1",
                    document_id=document_id,
                    source_format="xls",
                    text="Spreadsheet cell record",
                    element_type="table",
                    page_number=1,
                    workspace_id=workspace_id,
                ),
                Chunk(
                    id=f"{document_id}_other2",
                    document_id=document_id,
                    source_format="xls",
                    text="Spreadsheet summary row",
                    element_type="table",
                    page_number=1,
                    workspace_id=workspace_id,
                ),
            )
        return Document(
            id=document_id,
            filename=filename,
            source_format=suffix.replace(".", ""),
            chunks=chunks,
            workspace_id=workspace_id,
            incomplete_coverage_flag=False,
            exclusions=(),
        )

    def mock_embed(chunks: list[Any], provider: str = "prod") -> list[Any]:
        return chunks

    with patch("scripts.ingest_canonical_comparison_workspace.parse_file", side_effect=mock_parse_file), \
         patch("scripts.ingest_canonical_comparison_workspace.CloudRepository") as mock_cloud_cls, \
         patch("ingestion.embed_service.embed_chunks_dual", side_effect=mock_embed), \
         patch("ingestion.okf_builder.extract_and_persist_okf") as mock_okf:

        mock_cloud_inst = MagicMock()
        mock_cloud_cls.return_value = mock_cloud_inst

        mock_okf_manifest = MagicMock()
        mock_okf_manifest.status = "ready"
        mock_okf_manifest.entities = ["e1", "e2"]
        mock_okf_manifest.assertions = ["a1"]
        mock_okf.return_value = mock_okf_manifest

        results = ingest_comparison_corpus(
            workspace_id=ws_id,
            smoke_test=False,
        )

        # 1. PageIndex verification for PDFs
        assert len(results["page_index"]) == 6
        for pdf_name in [
            "2212.14776v3.pdf",
            "2412.20875v1.pdf",
            "2501.05730v1.pdf",
            "2501.09166v1.pdf",
            "SEC-Form-10Q.pdf",
            "National-Strategy-for-Artificial-Intelligence.pdf",
        ]:
            pi_info = results["page_index"][pdf_name]
            assert pi_info["status"] == "ready"
            assert pi_info["layout_available"] is True
            assert pi_info["elements_count"] >= 2
            assert "table 1" in pi_info["retrievable_targets"]
            assert "figure 2" in pi_info["retrievable_targets"]

        # 2. CSV snapshot row counts preserved from TableStore
        survay_snap = results["snapshots"]["survay.csv"]
        assert survay_snap["persisted_rows"] == 60255
        assert survay_snap["total_rows"] == 60255
        assert survay_snap["incomplete_coverage_flag"] is False
        assert survay_snap["page_index_status"] is None

        rs_snap = results["snapshots"]["rs_status_bill_passed_assent-1952-2016.csv"]
        assert rs_snap["persisted_rows"] == 727
        assert rs_snap["total_rows"] == 727
        assert rs_snap["incomplete_coverage_flag"] is False
        assert rs_snap["page_index_status"] is None

        # 3. Non CSV documents retain accurate parser chunk counts
        pdf_snap = results["snapshots"]["2212.14776v3.pdf"]
        assert pdf_snap["persisted_rows"] == 3
        assert pdf_snap["total_rows"] == 3
        assert pdf_snap["page_index_status"] == "ready"
        assert pdf_snap["page_index_elements_count"] >= 2

        xls_snap = results["snapshots"]["NFHS_5_India_Districts_Factsheet_Data.xls"]
        assert xls_snap["persisted_rows"] == 2
        assert xls_snap["total_rows"] == 2
        assert xls_snap["page_index_status"] is None

        # 4. Readiness summary reports complete verification
        assert results["readiness"]["tables_verified"] is True
        assert results["readiness"]["unavailable_page_index_layouts"] == []
        assert results["readiness"]["failed_enrichments"] == []


def test_missing_or_failed_page_index_cannot_be_reported_as_ready(isolated_smoke_env):
    """Verify missing layout or extraction failure is explicitly recorded and never reported as ready."""
    ws_id = "ws_test_pi_failure_reporting"

    def mock_parse_file(fpath: Path, document_id: str, filename: str, workspace_id: str) -> Document:
        # Paragraph only chunks: no titles, headings, tables or figures
        chunks = (
            Chunk(
                id=f"{document_id}_p1",
                document_id=document_id,
                source_format="pdf",
                text="Plain body paragraph text without any structural anchors or section titles.",
                element_type="paragraph",
                page_number=1,
                workspace_id=workspace_id,
            ),
        )
        return Document(
            id=document_id,
            filename=filename,
            source_format="pdf",
            chunks=chunks,
            workspace_id=workspace_id,
            incomplete_coverage_flag=False,
            exclusions=(),
        )

    with patch("scripts.ingest_canonical_comparison_workspace.parse_file", side_effect=mock_parse_file), \
         patch("scripts.ingest_canonical_comparison_workspace.CloudRepository") as mock_cloud_cls, \
         patch("ingestion.page_index_service.extract_pypdf_outline", return_value=([], 0)), \
         patch("ingestion.embed_service.embed_chunks_dual", side_effect=lambda chunks, provider='prod': chunks), \
         patch("ingestion.okf_builder.extract_and_persist_okf", return_value=None):

        mock_cloud_cls.return_value = MagicMock()

        results = ingest_comparison_corpus(
            workspace_id=ws_id,
            smoke_test=False,
        )

        for pdf_name in [
            "2212.14776v3.pdf",
            "2412.20875v1.pdf",
            "2501.05730v1.pdf",
            "2501.09166v1.pdf",
            "SEC-Form-10Q.pdf",
            "National-Strategy-for-Artificial-Intelligence.pdf",
        ]:
            pi_info = results["page_index"][pdf_name]
            assert pi_info["status"] != "ready"
            assert pi_info["layout_available"] is False
            assert pi_info["retrievable_targets"] == []

        assert len(results["readiness"]["unavailable_page_index_layouts"]) == 6
        assert len(results["readiness"]["failed_enrichments"]) >= 6


def test_missing_or_failed_okf_cannot_be_reported_as_ready(isolated_smoke_env):
    """Verify failed OKF extraction records failed status and prevents false ready claims."""
    ws_id = "ws_test_okf_failure_reporting"

    def mock_parse_file(fpath: Path, document_id: str, filename: str, workspace_id: str) -> Document:
        chunks = (
            Chunk(
                id=f"{document_id}_c1",
                document_id=document_id,
                source_format="pdf",
                text="Some document text",
                element_type="heading",
                page_number=1,
                workspace_id=workspace_id,
            ),
        )
        return Document(
            id=document_id,
            filename=filename,
            source_format="pdf",
            chunks=chunks,
            workspace_id=workspace_id,
            incomplete_coverage_flag=False,
            exclusions=(),
        )

    with patch("scripts.ingest_canonical_comparison_workspace.parse_file", side_effect=mock_parse_file), \
         patch("scripts.ingest_canonical_comparison_workspace.CloudRepository") as mock_cloud_cls, \
         patch("ingestion.embed_service.embed_chunks_dual", side_effect=lambda chunks, provider='prod': chunks), \
         patch("ingestion.okf_builder.extract_and_persist_okf", side_effect=RuntimeError("OKF budget exhausted")):

        mock_cloud_cls.return_value = MagicMock()

        results = ingest_comparison_corpus(
            workspace_id=ws_id,
            smoke_test=False,
        )

        for fname, snap in results["snapshots"].items():
            if fname.endswith(".pdf"):
                assert snap["okf_status"] == "failed"
                assert snap["okf_status"] != "ready"

        assert len(results["readiness"]["failed_enrichments"]) >= 6


def test_tablestore_manifest_mismatch_raises_and_blocks_readiness(isolated_smoke_env):
    """Verify TableStore manifest failure or row count mismatch raises ValueError."""
    with patch("scripts.ingest_canonical_comparison_workspace.ingest_csv_to_table_store") as mock_ingest_csv:
        mock_res = MagicMock()
        mock_res.persisted_rows = 500  # Expected 60255
        mock_res.source_rows = 60255
        mock_res.coverage_complete = True
        mock_res.document_version = "v_mismatch"
        mock_ingest_csv.return_value = mock_res

        with pytest.raises(AssertionError) as excinfo:
            ingest_comparison_corpus(
                workspace_id="ws_test_tablestore_mismatch",
                smoke_test=True,
            )
        assert "expected 60255 rows, got 500" in str(excinfo.value)
