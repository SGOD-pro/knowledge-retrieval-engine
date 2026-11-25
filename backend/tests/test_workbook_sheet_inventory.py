"""Offline tests for version-bound workbook sheet inventory, persistence, and grounded citations."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = BACKEND_ROOT / "src"
for p in (str(BACKEND_ROOT), str(SRC_ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.workspace_boundary_repo import (
    DynamoDBWorkspaceBoundaryRepository,
    InMemoryWorkspaceBoundaryRepository,
    SQLiteWorkspaceBoundaryRepository,
)
from schemas.models import Chunk
from services.evaluation.benchmark_scorer import evaluate_grounded_answer
from services.retrieval.evidence_contract import evidence_item_from_chunk
from services.retrieval.sheet_inventory import (
    build_sheet_inventory_chunk_and_evidence,
    lookup_sheet_status,
)
from src.schemas.contracts.boundary import (
    DocumentSnapshot,
    SheetInventoryEntry,
    WorkbookSheetInventory,
)


class _FakeXlsSheet:
    def __init__(self, name: str, rows: list[list[object]]) -> None:
        self.name = name
        self._rows = rows
        self.nrows = len(rows)
        self.ncols = max((len(r) for r in rows), default=0)

    def row_values(self, idx: int) -> list[object]:
        return self._rows[idx]


class _FakeXlsWorkbook:
    def __init__(self, sheets: list[_FakeXlsSheet]) -> None:
        self._sheets = sheets

    def sheets(self) -> list[_FakeXlsSheet]:
        return self._sheets


def _write_synthetic_xlsx(path: Path) -> bytes:
    """Create a real .xlsx file using openpyxl with:
    - Sheet1: populated (header + 2 data rows)
    - Sheet2: header-only (1 row, 0 data rows -> empty of data rows)
    - SheetEmpty: 0 cells (completely empty)
    """
    from openpyxl import Workbook

    wb = Workbook()
    s1 = wb.active
    s1.title = "Sheet1"
    s1.append(["Metric", "Value"])
    s1.append(["Revenue", 12500])
    s1.append(["Profit", 3400])

    s2 = wb.create_sheet("Sheet2")
    s2.append(["Metric", "Value"])

    wb.create_sheet("SheetEmpty")

    wb.save(str(path))
    return path.read_bytes()


def test_xls_and_xlsx_parse_and_extract_sheet_inventory(tmp_path: Path):
    from ingestion.parse_service import parse_file

    xls_path = tmp_path / "report_2025.xls"
    xls_bytes = b"synthetic-xls-bytes-v1"
    xls_path.write_bytes(xls_bytes)
    xls_ver = hashlib.sha256(xls_bytes).hexdigest()[:32]

    fake_wb = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("Sheet1", [["Metric", "Value"], ["Revenue", 12500], ["Profit", 3400]]),
            _FakeXlsSheet("Sheet2", [["Metric", "Value"]]),  # header-only -> empty
            _FakeXlsSheet("SheetEmpty", []),  # 0 rows/cols -> empty
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_wb):
        xls_doc = parse_file(xls_path, document_id="doc-xls-1", workspace_id="ws-1")

    # Existing row chunk IDs preserved
    assert [c.id for c in xls_doc.chunks] == [
        "doc-xls-1:xls:Sheet1:row1",
        "doc-xls-1:xls:Sheet1:row2",
    ]
    assert xls_doc.sheet_inventory is not None
    assert xls_doc.sheet_inventory["version"] == xls_ver
    assert xls_doc.sheet_inventory["complete"] is True

    xlsx_path = tmp_path / "report_2025.xlsx"
    xlsx_bytes = _write_synthetic_xlsx(xlsx_path)
    xlsx_ver = hashlib.sha256(xlsx_bytes).hexdigest()[:32]

    xlsx_doc = parse_file(xlsx_path, document_id="doc-xlsx-1", workspace_id="ws-1")
    assert any(c.id == "doc-xlsx-1:xlsx:Sheet1:A2" for c in xlsx_doc.chunks)
    assert xlsx_doc.sheet_inventory is not None
    assert xlsx_doc.sheet_inventory["version"] == xlsx_ver
    assert xlsx_doc.sheet_inventory["complete"] is True

    for fmt, doc, ver in (("xls", xls_doc, xls_ver), ("xlsx", xlsx_doc, xlsx_ver)):
        snap = DocumentSnapshot(
            workspace_id="ws-1",
            document_id=doc.id,
            source=doc.filename,
            version=ver,
            format=fmt,
            created_at="2026-11-25T00:00:00Z",
            sheet_inventory=doc.sheet_inventory,
        )
        assert lookup_sheet_status(snap, "Sheet1")["status"] == "populated"
        s2_res = lookup_sheet_status(snap, "Sheet2")
        assert s2_res["status"] == "empty"
        assert s2_res["header_only"] is True
        se_res = lookup_sheet_status(snap, "SheetEmpty")
        assert se_res["status"] == "empty"
        assert se_res["header_only"] is False
        assert lookup_sheet_status(snap, "Sheet3")["status"] == "absent"


def test_missing_incomplete_mismatched_and_unauthorized_inventory_returns_unknown():
    # 1. Legacy snapshot without sheet_inventory -> unknown
    legacy_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="legacy.xls",
        version="v1",
        format="xls",
        created_at="2026-11-25T00:00:00Z",
    )
    assert legacy_snap.sheet_inventory is None
    assert lookup_sheet_status(legacy_snap, "Sheet2")["status"] == "unknown"
    assert lookup_sheet_status(legacy_snap, "Sheet3")["status"] == "unknown"

    # 2. Incomplete inventory -> unknown (never absent)
    incomplete_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="partial.xls",
        version="v1",
        format="xls",
        incomplete_coverage_flag=False,
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=WorkbookSheetInventory(
            version="v1",
            complete=False,
            sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2),),
        ),
    )
    assert lookup_sheet_status(incomplete_snap, "Sheet3")["status"] == "unknown"

    # 3. incomplete_coverage_flag=True on snapshot -> unknown
    cov_flag_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="partial2.xls",
        version="v1",
        format="xls",
        incomplete_coverage_flag=True,
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=WorkbookSheetInventory(
            version="v1",
            complete=True,
            sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2),),
        ),
    )
    assert lookup_sheet_status(cov_flag_snap, "Sheet3")["status"] == "unknown"

    # 4. Version mismatch between snapshot and inventory -> unknown
    stale_inv_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="stale.xls",
        version="v2",
        format="xls",
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=WorkbookSheetInventory(
            version="v1",
            complete=True,
            sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2),),
        ),
    )
    assert lookup_sheet_status(stale_inv_snap, "Sheet3")["status"] == "unknown"

    # 5. Wrong workspace, out-of-scope, or tombstoned source -> unknown & no evidence
    valid_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="active.xls",
        version="v1",
        format="xls",
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=WorkbookSheetInventory(
            version="v1",
            complete=True,
            sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2),),
        ),
    )
    assert lookup_sheet_status(valid_snap, "Sheet1", workspace_id="ws-other")["status"] == "unknown"
    assert lookup_sheet_status(valid_snap, "Sheet1", allowed_document_ids={"doc-other"})["status"] == "unknown"

    mem_repo = InMemoryWorkspaceBoundaryRepository()
    mem_repo.create_workspace("WS1", "owner-1", workspace_id="ws_1")
    mem_repo.create_source("ws_1", "active.xls", source_id="doc-1")
    valid_ws_snap = valid_snap.model_copy(update={"workspace_id": "ws_1"})
    mem_repo.save_document_snapshot(valid_ws_snap)
    mem_repo.record_source_tombstone("ws_1", "doc-1", "owner-1")
    assert lookup_sheet_status(valid_ws_snap, "Sheet1", workspace_id="ws_1", boundary_repo=mem_repo)["status"] == "unknown"
    assert build_sheet_inventory_chunk_and_evidence(valid_ws_snap, workspace_id="ws_1", boundary_repo=mem_repo) is None


def test_persistence_roundtrip_inmemory_sqlite_and_dynamodb_and_legacy_readability(tmp_path: Path):
    inv = WorkbookSheetInventory(
        version="ver-abc",
        complete=True,
        sheets=(
            SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2, header_only=False),
            SheetInventoryEntry(name="Sheet2", status="empty", data_rows=0, header_only=True),
        ),
    )
    snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-1",
        source="wb.xls",
        version="ver-abc",
        format="xls",
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=inv,
    )

    # 1. InMemory
    mem_repo = InMemoryWorkspaceBoundaryRepository()
    mem_repo.save_document_snapshot(snap)
    got_mem = mem_repo.get_document_snapshot("ws-1", "doc-1")
    assert got_mem is not None and got_mem.sheet_inventory == inv

    # 2. SQLite (including legacy row without sheet_inventory_json)
    db_path = tmp_path / "boundary.db"
    sql_repo = SQLiteWorkspaceBoundaryRepository(db_path=str(db_path))
    sql_repo.save_document_snapshot(snap)
    got_sql = sql_repo.get_document_snapshot("ws-1", "doc-1")
    assert got_sql is not None and got_sql.sheet_inventory == inv
    assert sql_repo.list_document_snapshots("ws-1")[0].sheet_inventory == inv

    legacy_snap = DocumentSnapshot(
        workspace_id="ws-1",
        document_id="doc-legacy",
        source="legacy.xls",
        version="ver-old",
        format="xls",
        created_at="2026-11-25T00:00:00Z",
    )
    sql_repo.save_document_snapshot(legacy_snap)
    got_legacy_sql = sql_repo.get_document_snapshot("ws-1", "doc-legacy")
    assert got_legacy_sql is not None and got_legacy_sql.sheet_inventory is None

    # 3. DynamoDB (mocked table)
    stored_items: dict[tuple[str, str], dict] = {}
    mock_table = MagicMock()

    def _put_item(Item):
        stored_items[(Item["PK"], Item["SK"])] = dict(Item)

    def _get_item(Key):
        it = stored_items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def _query(**kwargs):
        return {"Items": [dict(v) for (pk, sk), v in stored_items.items() if sk.startswith("DOC_SNAPSHOT#")]}

    mock_table.put_item.side_effect = _put_item
    mock_table.get_item.side_effect = _get_item
    mock_table.query.side_effect = _query

    ddb_repo = DynamoDBWorkspaceBoundaryRepository.__new__(DynamoDBWorkspaceBoundaryRepository)
    with patch.object(DynamoDBWorkspaceBoundaryRepository, "table", new_callable=PropertyMock, return_value=mock_table):
        ddb_repo.save_document_snapshot(snap)
        ddb_repo.save_document_snapshot(legacy_snap)

        got_ddb = ddb_repo.get_document_snapshot("ws-1", "doc-1")
        assert got_ddb is not None and got_ddb.sheet_inventory == inv
        got_legacy_ddb = ddb_repo.get_document_snapshot("ws-1", "doc-legacy")
        assert got_legacy_ddb is not None and got_legacy_ddb.sheet_inventory is None
        listed_ddb = {s.document_id: s for s in ddb_repo.list_document_snapshots("ws-1")}
        assert listed_ddb["doc-1"].sheet_inventory == inv
        assert listed_ddb["doc-legacy"].sheet_inventory is None


def test_grounded_citation_path_with_row_and_inventory_evidence_items():
    """Verify existing citation & scorer path with a real row citation + inventory evidence item."""
    ws_id = "ws_inv_test"
    doc_id = "doc-wb-1"
    doc_fname = "Health_Districts_Data.xls"
    doc_ver = "v_active_001"

    repo = InMemoryWorkspaceBoundaryRepository()
    repo.create_workspace("Test", "owner-1", workspace_id=ws_id)
    repo.create_source(ws_id, doc_fname, source_id=doc_id)
    snap = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=doc_id,
        source=doc_fname,
        version=doc_ver,
        format="xls",
        created_at="2026-11-25T00:00:00Z",
        sheet_inventory=WorkbookSheetInventory(
            version=doc_ver,
            complete=True,
            sheets=(
                SheetInventoryEntry(name="Sheet1", status="populated", data_rows=2, header_only=False),
                SheetInventoryEntry(name="Sheet2", status="empty", data_rows=0, header_only=True),
            ),
        ),
    )
    repo.save_document_snapshot(snap)

    # Unversioned legacy row chunk must never have version inferred
    unversioned_row = Chunk(
        id=f"{doc_id}:xls:Sheet1:row1",
        document_id=doc_id,
        source_format="xls",
        text="Sheet1 row 2: District=North; Indicator=12500",
        element_type="row",
        section_path=("Sheet1",),
        location_reference="Sheet1 row 2",
        workspace_id=ws_id,
        metadata={"sheet": "Sheet1", "row": 2},
    )
    unver_ev = evidence_item_from_chunk(unversioned_row, workspace_id=ws_id, strategy="bm25", boundary_repo=repo)
    assert unver_ev.document_version is None

    # Version-bound row chunk + version-bound inventory evidence item
    row_chunk = Chunk(
        id=f"{doc_id}:xls:Sheet1:row1",
        document_id=doc_id,
        source_format="xls",
        text="Sheet1 row 2: District=North; Indicator=12500",
        element_type="row",
        section_path=("Sheet1",),
        location_reference="Sheet1 row 2",
        workspace_id=ws_id,
        version=doc_ver,
        document_version=doc_ver,
        metadata={"sheet": "Sheet1", "row": 2, "document_version": doc_ver},
    )
    row_ev = evidence_item_from_chunk(row_chunk, workspace_id=ws_id, strategy="bm25", boundary_repo=repo)

    inv_pair = build_sheet_inventory_chunk_and_evidence(snap, workspace_id=ws_id, boundary_repo=repo)
    assert inv_pair is not None
    inv_chunk, inv_ev = inv_pair

    retained = [row_ev, inv_ev]
    context_ids = [row_chunk.id, inv_chunk.id]
    gt_ids = [row_chunk.id]

    row_cit = {
        "chunk_id": row_chunk.id,
        "document_id": doc_id,
        "document_filename": doc_fname,
        "document_version": doc_ver,
        "location_reference": "Sheet1 row 2",
        "snippet": row_chunk.text,
    }
    inv_cit = {
        "chunk_id": inv_chunk.id,
        "document_id": doc_id,
        "document_filename": doc_fname,
        "document_version": doc_ver,
        "location_reference": "Sheet Inventory",
        "snippet": inv_chunk.text,
    }

    full_answer = "In Sheet1, North District has Indicator 12500, while Sheet2 is empty with 0 data rows."

    # 1. Both required evidence items present and cited -> PASS
    res_both = evaluate_grounded_answer(
        answer=full_answer,
        citations=[row_cit, inv_cit],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[doc_fname],
        expected_source_version=doc_ver,
        retained_evidence_items=retained,
        context_chunk_ids=context_ids,
        ground_truth_chunk_ids=gt_ids,
    )
    assert res_both["is_grounded_correct"] is True, f"Unexpected failure: {res_both}"

    # 2. Removing the row evidence item prevents a grounded full answer
    res_no_row = evaluate_grounded_answer(
        answer=full_answer,
        citations=[inv_cit],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[doc_fname],
        expected_source_version=doc_ver,
        retained_evidence_items=[inv_ev],
        context_chunk_ids=[inv_chunk.id],
        ground_truth_chunk_ids=gt_ids,
    )
    assert res_no_row["is_grounded_correct"] is False

    # 3. Removing the inventory evidence item prevents a grounded full answer about empty Sheet2
    # (when answer asserts Sheet2 is empty, row_chunk alone does not mention Sheet2 or empty)
    empty_sheet_answer = "Sheet2 is empty with 0 data rows and contains no data."
    res_no_inv = evaluate_grounded_answer(
        answer=empty_sheet_answer,
        citations=[row_cit],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[doc_fname],
        expected_source_version=doc_ver,
        retained_evidence_items=[row_ev],
        context_chunk_ids=[row_chunk.id],
        ground_truth_chunk_ids=gt_ids,
    )
    assert res_no_inv["is_grounded_correct"] is False

    # 4. Unknown status preserves scoped refusal with zero citations
    res_refusal = evaluate_grounded_answer(
        answer="NOT_FOUND",
        citations=[],
        is_answer_text_correct=True,
        contract_type="refusal",
        ans_is_refusal=True,
    )
    assert res_refusal["is_grounded_correct"] is True
    assert res_refusal["reason"] == "valid_unsupported_refusal"


def test_xls_and_xlsx_first_row_data_vs_header_only_and_ambiguous(tmp_path: Path):
    """Verify blank, genuine header-only, first-row data, ambiguous first row, populated, and absent sheets."""
    from openpyxl import Workbook
    from ingestion.parse_service import parse_file

    # XLSX workbook
    xlsx_path = tmp_path / "first_row_cases.xlsx"
    wb = Workbook()
    s_blank = wb.active
    s_blank.title = "BlankSheet"
    s_hdr = wb.create_sheet("HeaderOnly")
    s_hdr.append(["Metric", "Value"])
    s_fr = wb.create_sheet("FirstRowData")
    s_fr.append(["Total Households", 1292])
    s_amb = wb.create_sheet("AmbiguousFirstRow")
    s_amb.append(["This single row contains an explanatory note rather than column headers."])
    s_pop = wb.create_sheet("Populated")
    s_pop.append(["District", "Households"])
    s_pop.append(["North", 882])
    wb.save(str(xlsx_path))

    xlsx_doc = parse_file(xlsx_path, document_id="doc-xlsx-cases", workspace_id="ws_1")
    # Preserve existing cell IDs
    assert any(c.id == "doc-xlsx-cases:xlsx:FirstRowData:A1" for c in xlsx_doc.chunks)
    assert any(c.id == "doc-xlsx-cases:xlsx:Populated:B2" for c in xlsx_doc.chunks)
    xlsx_snap = DocumentSnapshot(
        workspace_id="ws_1",
        document_id=xlsx_doc.id,
        source=xlsx_doc.filename,
        version=xlsx_doc.sheet_inventory["version"],
        format="xlsx",
        created_at="2026-11-26T00:00:00Z",
        sheet_inventory=xlsx_doc.sheet_inventory,
    )
    assert lookup_sheet_status(xlsx_snap, "BlankSheet")["status"] == "empty"
    assert lookup_sheet_status(xlsx_snap, "BlankSheet")["header_only"] is False
    assert lookup_sheet_status(xlsx_snap, "HeaderOnly")["status"] == "empty"
    assert lookup_sheet_status(xlsx_snap, "HeaderOnly")["header_only"] is True
    assert lookup_sheet_status(xlsx_snap, "FirstRowData")["status"] == "populated"
    assert lookup_sheet_status(xlsx_snap, "AmbiguousFirstRow")["status"] == "unknown"
    assert lookup_sheet_status(xlsx_snap, "Populated")["status"] == "populated"
    assert lookup_sheet_status(xlsx_snap, "MissingSheet")["status"] == "absent"

    # XLS workbook
    xls_path = tmp_path / "first_row_cases.xls"
    xls_bytes = b"synthetic-xls-first-row-cases"
    xls_path.write_bytes(xls_bytes)
    fake_xls = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("BlankSheet", []),
            _FakeXlsSheet("HeaderOnly", [["Metric", "Value"]]),
            _FakeXlsSheet("FirstRowData", [["Total Households", 1292]]),
            _FakeXlsSheet("AmbiguousFirstRow", [["This single row contains an explanatory note rather than column headers."]]),
            _FakeXlsSheet("Populated", [["District", "Households"], ["North", 882]]),
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_xls):
        xls_doc = parse_file(xls_path, document_id="doc-xls-cases", workspace_id="ws_1")

    assert [c.id for c in xls_doc.chunks] == ["doc-xls-cases:xls:Populated:row1"]
    xls_snap = DocumentSnapshot(
        workspace_id="ws_1",
        document_id=xls_doc.id,
        source=xls_doc.filename,
        version=xls_doc.sheet_inventory["version"],
        format="xls",
        created_at="2026-11-26T00:00:00Z",
        sheet_inventory=xls_doc.sheet_inventory,
    )
    assert lookup_sheet_status(xls_snap, "BlankSheet")["status"] == "empty"
    assert lookup_sheet_status(xls_snap, "HeaderOnly")["status"] == "empty"
    assert lookup_sheet_status(xls_snap, "HeaderOnly")["header_only"] is True
    assert lookup_sheet_status(xls_snap, "FirstRowData")["status"] == "populated"
    assert lookup_sheet_status(xls_snap, "AmbiguousFirstRow")["status"] == "unknown"
    assert lookup_sheet_status(xls_snap, "Populated")["status"] == "populated"
    assert lookup_sheet_status(xls_snap, "MissingSheet")["status"] == "absent"


def test_query_service_end_to_end_workbook_inventory_and_negative_blocking(tmp_path: Path):
    """Prove normal QueryService path cites row + inventory, passes strict scorer, and blocks unsupported claims."""
    from ingestion.parse_service import parse_file
    from modules.query.query_service import QueryRequest, QueryService
    from services.retrieval import bm25_retriever

    ws_id = "ws_qs_e2e"
    xls_path = tmp_path / "district_survey_2025.xls"
    xls_bytes = b"synthetic-xls-e2e-bytes"
    xls_path.write_bytes(xls_bytes)
    xls_ver = hashlib.sha256(xls_bytes).hexdigest()[:32]
    fake_xls = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("Sheet1", [["District", "Households"], ["North", 882], ["South", 410]]),
            _FakeXlsSheet("Sheet2", [["District", "Households"]]),
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_xls):
        xls_doc = parse_file(xls_path, document_id="doc-xls-e2e", workspace_id=ws_id)

    # Simulate legacy chunk missing filename/version metadata on row chunks
    legacy_chunks = tuple(
        Chunk(
            id=c.id,
            document_id=c.document_id,
            source_format=c.source_format,
            text=c.text,
            element_type=c.element_type,
            section_path=c.section_path,
            location_reference=c.location_reference,
            metadata={"sheet": "Sheet1", "row": 2},
            workspace_id=ws_id,
        )
        for c in xls_doc.chunks
    )

    class _MemDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": xls_doc.id, "filename": xls_doc.filename}]}

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            if document_ids and xls_doc.id not in document_ids:
                return []
            return list(legacy_chunks)

        def get(self, doc_id: str):
            return xls_doc if doc_id == xls_doc.id else None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    doc_repo = _MemDocRepo()
    row_cid = "doc-xls-e2e:xls:Sheet1:row1"
    inv_cid = f"doc-xls-e2e:sheet_inventory:{xls_ver}"
    query = "In district_survey_2025.xls, how many Households are in North in Sheet1 and what is in Sheet2?"
    ans_text = "In Sheet1, North has 882 Households, and Sheet2 is empty with 0 data rows."

    def _fake_rerank(q, chunks, top_k=6, **kwargs):
        return list(chunks)[:top_k], "bypassed", {"mode": "bypassed", "candidate_count": len(chunks)}

    def _run_qs(snap: DocumentSnapshot | None, *, tombstone: bool = False, llm_cits: list[str]):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace("E2E WS", "owner-1", workspace_id=ws_id)
        b_repo.create_source(ws_id, xls_doc.filename, source_id=xls_doc.id)
        if snap is not None:
            b_repo.save_document_snapshot(snap)
        if tombstone:
            b_repo.record_source_tombstone(ws_id, xls_doc.id, "owner-1")

        qs = QueryService(repo=doc_repo)
        req = QueryRequest(query=query, workspace_id=ws_id, benchmark_mode=True, cache=False, force_full_path=True)
        llm_payload = json.dumps({"answer": ans_text, "citations": llm_cits})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.rerank", side_effect=_fake_rerank),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(llm_payload, {"input_tokens": 50, "output_tokens": 20})),
        ):
            return qs.execute_query(req)

    valid_snap = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=xls_doc.id,
        source=xls_doc.filename,
        version=xls_ver,
        format="xls",
        created_at="2026-11-26T00:00:00Z",
        sheet_inventory=xls_doc.sheet_inventory,
    )
    res_ok = _run_qs(valid_snap, llm_cits=[row_cid, inv_cid])
    assert res_ok["answer"] == ans_text
    assert [c["chunk_id"] for c in res_ok["citations"]] == [row_cid, inv_cid]
    eval_ok = evaluate_grounded_answer(
        answer=res_ok["answer"],
        citations=res_ok["citations"],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[xls_doc.filename],
        expected_source_version=xls_ver,
        retained_evidence_items=res_ok["retained_evidence_items"],
        context_chunk_ids=res_ok.get("retrieval_candidates", {}).get("compressed_context"),
        ground_truth_chunk_ids=[row_cid],
        query=query,
    )
    assert eval_ok["is_grounded_correct"] is True

    # Negative cases: missing, incomplete, stale inventory with mocked LLM asserting empty Sheet2 using only row_cid
    snap_missing = valid_snap.model_copy(update={"sheet_inventory": None})
    snap_incomplete = valid_snap.model_copy(
        update={"sheet_inventory": WorkbookSheetInventory(version=xls_ver, complete=False, sheets=valid_snap.sheet_inventory.sheets)}
    )
    snap_stale = valid_snap.model_copy(
        update={"sheet_inventory": WorkbookSheetInventory(version="stale_ver", complete=True, sheets=valid_snap.sheet_inventory.sheets)}
    )
    for neg_snap, tomb in ((snap_missing, False), (snap_incomplete, False), (snap_stale, False), (valid_snap, True)):
        neg_res = _run_qs(neg_snap, tombstone=tomb, llm_cits=[row_cid])
        assert neg_res["citations"] == []
        assert "empty" not in neg_res["answer"].lower()


def test_conditional_inventory_backfill_primitive_across_backends(tmp_path: Path):
    """Prove atomic conditional inventory backfill across InMemory, SQLite, and DynamoDB."""
    from src.schemas.contracts.boundary import CASConflictError

    inv1 = WorkbookSheetInventory(
        version="v1",
        complete=True,
        sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=5),),
    )
    inv2 = WorkbookSheetInventory(
        version="v1",
        complete=True,
        sheets=(SheetInventoryEntry(name="Sheet1", status="populated", data_rows=6),),
    )
    base_snap = DocumentSnapshot(
        workspace_id="ws_bf",
        document_id="doc_bf",
        source="wb.xls",
        version="v1",
        format="xls",
        persisted_rows=5,
        total_rows=5,
        created_at="2026-11-26T00:00:00Z",
        page_index_status="ready",
        page_index_elements_count=12,
        okf_status="ready",
    )

    def _exercise_repo(repo, *, is_ddb=False):
        repo.create_workspace("BF", "owner-1", workspace_id="ws_bf")
        repo.create_source("ws_bf", "wb.xls", source_id="doc_bf")
        repo.save_document_snapshot(base_snap)

        # 1. First write succeeds and preserves unrelated fields
        updated = repo.update_document_snapshot_inventory("ws_bf", "doc_bf", "v1", inv1)
        assert updated.sheet_inventory == inv1
        assert updated.persisted_rows == 5
        assert updated.page_index_status == "ready"
        assert updated.page_index_elements_count == 12
        assert updated.okf_status == "ready"

        # 2. Identical retry is idempotent
        retried = repo.update_document_snapshot_inventory("ws_bf", "doc_bf", "v1", inv1)
        assert retried.sheet_inventory == inv1

        # 3. Different existing inventory is rejected
        with pytest.raises(CASConflictError):
            repo.update_document_snapshot_inventory("ws_bf", "doc_bf", "v1", inv2)

        # 4. Concurrent version replacement is rejected
        newer_snap = base_snap.model_copy(update={"version": "v2", "sheet_inventory": None})
        repo.save_document_snapshot(newer_snap)
        with pytest.raises(CASConflictError):
            repo.update_document_snapshot_inventory("ws_bf", "doc_bf", "v1", inv1)

        # 5. Source revocation and workspace revocation are rejected
        v2_inv = WorkbookSheetInventory(version="v2", complete=True, sheets=inv1.sheets)
        repo.record_source_tombstone("ws_bf", "doc_bf", "owner-1")
        with pytest.raises(CASConflictError):
            repo.update_document_snapshot_inventory("ws_bf", "doc_bf", "v2", v2_inv)

    # InMemory
    _exercise_repo(InMemoryWorkspaceBoundaryRepository())

    # SQLite
    _exercise_repo(SQLiteWorkspaceBoundaryRepository(db_path=str(tmp_path / "bf.db")))

    # DynamoDB (atomic transact_write_items simulation)
    stored: dict[tuple[str, str], dict] = {}
    mock_table = MagicMock()
    mock_table.table_name = "boundary-table"

    def _put_item(Item, **kwargs):
        stored[(Item["PK"], Item["SK"])] = dict(Item)

    def _get_item(Key, **kwargs):
        it = stored.get((Key["PK"], Key["SK"]))
        return {"Item": dict(it)} if it else {}

    def _transact_write_items(TransactItems):
        # Validate all conditions before mutating (atomic)
        for ti in TransactItems:
            if "ConditionCheck" in ti:
                cc = ti["ConditionCheck"]
                k = (cc["Key"]["PK"], cc["Key"]["SK"])
                if "attribute_not_exists(PK)" in cc["ConditionExpression"] and k in stored:
                    raise RuntimeError("TransactionCanceledException: tombstone exists")
            elif "Update" in ti:
                up = ti["Update"]
                k = (up["Key"]["PK"], up["Key"]["SK"])
                cur = stored.get(k)
                vals = up["ExpressionAttributeValues"]
                if cur is None or cur.get("version") != vals[":ver"]:
                    raise RuntimeError("TransactionCanceledException: version mismatch")
                cur_inv = cur.get("sheet_inventory_json")
                if cur_inv is not None and cur_inv != vals[":inv"]:
                    raise RuntimeError("TransactionCanceledException: different inventory exists")
        for ti in TransactItems:
            if "Update" in ti:
                up = ti["Update"]
                k = (up["Key"]["PK"], up["Key"]["SK"])
                stored[k]["sheet_inventory_json"] = up["ExpressionAttributeValues"][":inv"]

    class _TxCanceled(Exception):
        pass

    def _tx_wrapper(TransactItems):
        try:
            _transact_write_items(TransactItems)
        except RuntimeError as e:
            err = _TxCanceled(str(e))
            err.response = {"Error": {"Code": "TransactionCanceledException"}}
            raise err from e

    mock_table.put_item.side_effect = _put_item
    mock_table.get_item.side_effect = _get_item
    mock_table.meta.client.transact_write_items.side_effect = _tx_wrapper

    ddb_repo = DynamoDBWorkspaceBoundaryRepository.__new__(DynamoDBWorkspaceBoundaryRepository)
    with patch.object(DynamoDBWorkspaceBoundaryRepository, "table", new_callable=PropertyMock, return_value=mock_table):
        _exercise_repo(ddb_repo, is_ddb=True)
        # Also verify workspace tombstone rejection
        ddb_repo.save_document_snapshot(base_snap.model_copy(update={"workspace_id": "ws_bf2", "document_id": "doc_ws_tomb"}))
        ddb_repo.create_workspace("BF2", "owner-1", workspace_id="ws_bf2")
        ddb_repo.tombstone_workspace("ws_bf2", "owner-1")
        with pytest.raises(CASConflictError):
            ddb_repo.update_document_snapshot_inventory("ws_bf2", "doc_ws_tomb", "v1", inv1)


def test_workbook_row_survives_competing_pdfs_and_rejects_unrelated_or_missing_citations(tmp_path: Path, monkeypatch):
    """Reproduce Q150 failure: competing PDFs crowd out Sheet1 row, and missing/unrelated citations leak PDFs."""
    from ingestion.parse_service import parse_file
    from modules.query.query_service import QueryRequest, QueryService
    from services.retrieval import bm25_retriever

    monkeypatch.setenv("BEDROCK_RERANK_ENABLED", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "")

    ws_id = "ws_q150_repro"
    xls_path = tmp_path / "NFHS_Test_Workbook.xls"
    xls_bytes = b"synthetic-nfhs-workbook-v1"
    xls_path.write_bytes(xls_bytes)
    xls_ver = hashlib.sha256(xls_bytes).hexdigest()[:32]
    fake_xls = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("Sheet1", [["District Names", "State/UT", "Households"], ["Nicobars", "AN", 882]]),
            _FakeXlsSheet("Sheet2", []),
            _FakeXlsSheet("Sheet3", []),
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_xls):
        wb_doc = parse_file(xls_path, document_id="doc-wb-q150", workspace_id=ws_id)

    row_cid = "doc-wb-q150:xls:Sheet1:row1"
    inv_cid = f"doc-wb-q150:sheet_inventory:{xls_ver}"

    # 12 competing PDF chunks mentioning dataset / populated / Sheet2 / Sheet3
    pdf_chunks = [
        Chunk(
            id=f"doc-pdf-{i}:page:{i+1}:element:{i}",
            document_id=f"doc-pdf-{i}",
            source_format="pdf",
            text=f"Populated dataset analysis Sheet2 Sheet3 district datasets comparison {i}.",
            element_type="text",
            page_number=i + 1,
            workspace_id=ws_id,
        )
        for i in range(12)
    ]

    all_chunks = list(wb_doc.chunks) + pdf_chunks

    class _CrowdDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {
                "documents": [{"id": wb_doc.id, "filename": wb_doc.filename}]
                + [{"id": f"doc-pdf-{i}", "filename": f"Distractor_{i}.pdf"} for i in range(12)]
            }

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            if document_ids is not None:
                ds = set(document_ids)
                return [c for c in all_chunks if c.document_id in ds]
            return list(all_chunks)

        def get(self, doc_id: str):
            return wb_doc if doc_id == wb_doc.id else None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Q150 Repro", "owner-1", workspace_id=ws_id)
    b_repo.create_source(ws_id, wb_doc.filename, source_id=wb_doc.id)
    snap = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=wb_doc.id,
        source=wb_doc.filename,
        version=xls_ver,
        format="xls",
        created_at="2026-11-26T00:00:00Z",
        sheet_inventory=wb_doc.sheet_inventory,
    )
    b_repo.save_document_snapshot(snap)
    for i in range(12):
        b_repo.create_source(ws_id, f"Distractor_{i}.pdf", source_id=f"doc-pdf-{i}")
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws_id,
                document_id=f"doc-pdf-{i}",
                source=f"Distractor_{i}.pdf",
                version=f"vpdf{i}",
                format="pdf",
                created_at="2026-11-26T00:00:00Z",
            )
        )

    doc_repo = _CrowdDocRepo()
    query = "Are Sheet2 and Sheet3 additional populated district datasets in the NFHS_Test_Workbook.xls workbook?"
    ans_text = "No. Sheet2 and Sheet3 are empty (0 data rows); district data for Nicobars (882 Households) are on Sheet1."

    def _run(llm_raw_resp: str):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        qs = QueryService(repo=doc_repo)
        req = QueryRequest(query=query, workspace_id=ws_id, benchmark_mode=True, cache=False, force_full_path=True)
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(llm_raw_resp, {"input_tokens": 50, "output_tokens": 20})),
        ):
            return qs.execute_query(req)

    # 1. Missing citation output must NOT dump all 6 context chunks (no unrelated PDFs cited)
    res_missing_cit = _run(json.dumps({"answer": ans_text, "citations": []}))
    assert not any(c.get("source_format") == "pdf" for c in res_missing_cit.get("citations", []))
    assert res_missing_cit.get("citations") == []

    # 2. Explicit unrelated PDF citation alongside inventory must NOT admit PDF or pass without row support
    res_unrel_cit = _run(json.dumps({"answer": ans_text, "citations": [inv_cid, "doc-pdf-0:page:1:element:0"]}))
    assert not any(c.get("source_format") == "pdf" for c in res_unrel_cit.get("citations", []))

    # 3. Supported full answer citing both real Sheet1 row and inventory survives rerank and passes strict scorer
    res_full = _run(json.dumps({"answer": ans_text, "citations": [row_cid, inv_cid]}))
    comp_ids = (res_full.get("retrieval_candidates") or {}).get("compressed_context") or []
    assert row_cid in comp_ids, f"Sheet1 row {row_cid} lost before context: {comp_ids}"
    assert inv_cid in comp_ids, f"Inventory {inv_cid} lost before context: {comp_ids}"
    assert [c["chunk_id"] for c in res_full["citations"]] == [row_cid, inv_cid]

    eval_res = evaluate_grounded_answer(
        answer=res_full["answer"],
        citations=res_full["citations"],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[wb_doc.filename],
        expected_source_version=xls_ver,
        retained_evidence_items=res_full["retained_evidence_items"],
        context_chunk_ids=comp_ids,
        ground_truth_chunk_ids=[row_cid],
        query=query,
    )
    assert eval_res["is_grounded_correct"] is True, f"Scorer failed: {eval_res}"


def test_delivery_gate_blocks_paraphrased_and_invalid_citation_sheet_status_claims(tmp_path: Path, monkeypatch):
    """Reproduce probe_q150_verified_pass.json bug: paraphrased claim with empty or invalid citations."""
    from ingestion.parse_service import parse_file
    from modules.query.query_service import QueryRequest, QueryService
    from services.retrieval import bm25_retriever

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws_id = "ws_gate_paraphrase"
    xls_path = tmp_path / "Survey_Workbook.xls"
    xls_bytes = b"synthetic-paraphrase-wb"
    xls_path.write_bytes(xls_bytes)
    xls_ver = hashlib.sha256(xls_bytes).hexdigest()[:32]
    fake_wb = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("Sheet1", [["District", "Households"], ["Nicobars", 882]]),
            _FakeXlsSheet("Sheet2", []),
            _FakeXlsSheet("Sheet3", []),
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_wb):
        wb_doc = parse_file(xls_path, document_id="doc-para", workspace_id=ws_id)

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Para WS", "owner-1", workspace_id=ws_id)
    b_repo.create_source(ws_id, wb_doc.filename, source_id=wb_doc.id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=wb_doc.id,
            source=wb_doc.filename,
            version=xls_ver,
            format="xls",
            created_at="2026-11-26T00:00:00Z",
            sheet_inventory=wb_doc.sheet_inventory,
        )
    )

    class _DocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": wb_doc.id, "filename": wb_doc.filename}]}

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return list(wb_doc.chunks)

        def get(self, doc_id: str):
            return wb_doc if doc_id == wb_doc.id else None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    doc_repo = _DocRepo()
    query = "Are Sheet2 and Sheet3 additional populated district datasets in the Survey_Workbook.xls workbook?"
    paraphrase_ans = "Sheet2 and Sheet3 are not additional populated district datasets in the Survey_Workbook.xls workbook."

    for raw_cits in ([], ["nonexistent:chunk:999"]):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        qs = QueryService(repo=doc_repo)
        req = QueryRequest(query=query, workspace_id=ws_id, benchmark_mode=True, cache=False, force_full_path=True)
        payload = json.dumps({"answer": paraphrase_ans, "citations": raw_cits})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(payload, {"input_tokens": 10, "output_tokens": 10})),
        ):
            res = qs.execute_query(req)
        assert res["citations"] == []
        assert "not additional populated" not in res["answer"].lower()


def test_deterministic_sheet_status_path_zero_synthesis_and_strict_scorer(tmp_path: Path, monkeypatch):
    """Prove deterministic sheet-status path renders answer, emits row+inventory citations, and passes scorer with 0 LLM calls."""
    from ingestion.parse_service import parse_file
    from modules.query.query_service import QueryRequest, QueryService
    from services.retrieval import bm25_retriever

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    monkeypatch.setenv("ENABLE_DETERMINISTIC_MATH", "1")
    ws_id = "ws_det_sheet"
    xls_path = tmp_path / "District_Factsheet.xls"
    xls_bytes = b"synthetic-det-sheet-wb"
    xls_path.write_bytes(xls_bytes)
    xls_ver = hashlib.sha256(xls_bytes).hexdigest()[:32]
    fake_wb = _FakeXlsWorkbook(
        [
            _FakeXlsSheet("Sheet1", [["District", "Households"], ["Nicobars", 882]]),
            _FakeXlsSheet("Sheet2", []),
            _FakeXlsSheet("Sheet3", []),
        ]
    )
    with patch("xlrd.open_workbook", return_value=fake_wb):
        wb_doc = parse_file(xls_path, document_id="doc-det-wb", workspace_id=ws_id)

    row_cid = "doc-det-wb:xls:Sheet1:row1"
    inv_cid = f"doc-det-wb:sheet_inventory:{xls_ver}"

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Det WS", "owner-1", workspace_id=ws_id)
    b_repo.create_source(ws_id, wb_doc.filename, source_id=wb_doc.id)
    valid_snap = DocumentSnapshot(
        workspace_id=ws_id,
        document_id=wb_doc.id,
        source=wb_doc.filename,
        version=xls_ver,
        format="xls",
        created_at="2026-11-26T00:00:00Z",
        sheet_inventory=wb_doc.sheet_inventory,
    )
    b_repo.save_document_snapshot(valid_snap)

    class _DocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": wb_doc.id, "filename": wb_doc.filename}]}

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return list(wb_doc.chunks)

        def get(self, doc_id: str):
            return wb_doc if doc_id == wb_doc.id else None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    doc_repo = _DocRepo()
    query = "Are Sheet2 and Sheet3 additional populated district datasets in the District_Factsheet.xls workbook?"

    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    qs = QueryService(repo=doc_repo)
    req = QueryRequest(query=query, workspace_id=ws_id, benchmark_mode=True, cache=False, force_full_path=False)
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
        patch("providers.reranker_provider._call_openrouter_rerank", side_effect=AssertionError("Remote reranker must not be called in lexical mode"), create=True),
        patch("services.llm.llm_service.generate_completion", side_effect=AssertionError("LLM must not be called on deterministic sheet-status path")),
    ):
        res = qs.execute_query(req)

    ans_lower = res["answer"].lower()
    assert "sheet2" in ans_lower and "empty" in ans_lower
    assert "sheet3" in ans_lower
    assert "sheet1" in ans_lower and "populated" in ans_lower
    assert set(c["chunk_id"] for c in res["citations"]) == {row_cid, inv_cid}
    assert res["token_usage"] == {"input_tokens": 0, "output_tokens": 0}

    eval_res = evaluate_grounded_answer(
        answer=res["answer"],
        citations=res["citations"],
        is_answer_text_correct=True,
        contract_type="semantic",
        expected_sources=[wb_doc.filename],
        expected_source_version=xls_ver,
        retained_evidence_items=res["retained_evidence_items"],
        context_chunk_ids=(res.get("retrieval_candidates") or {}).get("compressed_context"),
        ground_truth_chunk_ids=[row_cid],
        query=query,
    )
    assert eval_res["is_grounded_correct"] is True, f"Scorer failed: {eval_res}"

    # Wrong-sheet row citation (Sheet9 row when Sheet1 is populated) must be rejected by verify_sheet_status_claims
    from services.retrieval.evidence_contract import evidence_item_from_chunk
    from services.retrieval.sheet_inventory import build_sheet_inventory_chunk_and_evidence, verify_sheet_status_claims

    wrong_sheet_chunk = Chunk(
        id="doc-det-wb:xls:Sheet9:row1",
        document_id=wb_doc.id,
        source_format="xls",
        text="Sheet9 row 2: something",
        element_type="row",
        section_path=("Sheet9",),
        location_reference="Sheet9 row 2",
        workspace_id=ws_id,
        version=xls_ver,
        document_version=xls_ver,
        metadata={"sheet": "Sheet9", "row": 2, "document_version": xls_ver},
    )
    wrong_ev = evidence_item_from_chunk(wrong_sheet_chunk, workspace_id=ws_id, strategy="bm25", boundary_repo=b_repo)
    _, inv_ev = build_sheet_inventory_chunk_and_evidence(valid_snap, workspace_id=ws_id, boundary_repo=b_repo)
    ok_wrong, reason_wrong = verify_sheet_status_claims(
        res["answer"],
        [wrong_ev, inv_ev],
        [{"chunk_id": wrong_sheet_chunk.id}, {"chunk_id": inv_cid}],
        query=query,
    )
    assert ok_wrong is False and reason_wrong == "missing_populated_sheet_row_support"

    # Absent sheet (Sheet4) on deterministic path
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    req_absent = QueryRequest(
        query="Is Sheet4 populated or absent in District_Factsheet.xls?",
        workspace_id=ws_id,
        benchmark_mode=True,
        cache=False,
        force_full_path=False,
    )
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
        patch("services.llm.llm_service.generate_completion", side_effect=AssertionError("LLM must not be called")),
    ):
        res_absent = qs.execute_query(req_absent)
    assert "sheet4 is absent" in res_absent["answer"].lower()
    assert set(c["chunk_id"] for c in res_absent["citations"]) == {row_cid, inv_cid}


def test_q194_json_dict_answer_preserves_type_and_enforces_workbook_claims(tmp_path: Path, monkeypatch):
    """Reproduce Q194 TypeError on dict answers and ensure structured sheet-status claims cannot bypass checks."""
    from modules.query.query_service import QueryRequest, QueryService
    from services.retrieval import bm25_retriever

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws_id = "ws_q194_json"
    c1 = Chunk(
        id="doc-json:page:1:element:0",
        document_id="doc-json",
        source_format="pdf",
        text="Report status is ok and item count is 5.",
        element_type="text",
        page_number=1,
        workspace_id=ws_id,
        version="v1",
        document_version="v1",
    )

    class _JsonDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {"documents": [{"id": "doc-json", "filename": "json_report.pdf"}]}

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return [c1]

        def get(self, doc_id: str):
            return None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("JSON WS", "owner-1", workspace_id=ws_id)
    b_repo.create_source(ws_id, "json_report.pdf", source_id="doc-json")
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id="doc-json",
            source="json_report.pdf",
            version="v1",
            format="pdf",
            created_at="2026-11-26T00:00:00Z",
        )
    )

    doc_repo = _JsonDocRepo()
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()

    qs = QueryService(repo=doc_repo)
    req = QueryRequest(
        query="Answer only as a JSON object with keys status and count.",
        workspace_id=ws_id,
        benchmark_mode=True,
        cache=False,
        force_full_path=True,
    )
    good_json = json.dumps({"answer": {"status": "ok", "count": 5}, "citations": [c1.id]})
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
        patch("services.langgraph_pipeline.check_fidelity", return_value=True),
        patch("services.llm.llm_service.generate_completion", return_value=(good_json, {"input_tokens": 10, "output_tokens": 10})),
    ):
        res_ok = qs.execute_query(req)

    assert isinstance(res_ok["answer"], dict)
    assert res_ok["answer"] == {"status": "ok", "count": 5}
    assert [c["chunk_id"] for c in res_ok["citations"]] == [c1.id]

    from services.evaluation.benchmark_scorer import evaluate_grounded_answer

    ev_q194 = evaluate_grounded_answer(
        answer=json.dumps(res_ok["answer"]),
        citations=res_ok["citations"],
        is_answer_text_correct=True,
        contract_type="json_schema",
        contract={"contract_type": "json_schema", "required_keys": ["status", "count"]},
        expected_sources=["json_report.pdf"],
        expected_source_version="v1",
        retained_evidence_items=res_ok.get("retained_evidence_items"),
        context_chunk_ids=res_ok.get("compressed_context_chunk_ids"),
        ground_truth_chunk_ids=[c1.id],
        outcome=res_ok,
        query=req.query,
    )
    assert ev_q194["is_grounded_correct"] is True

    # Negative case: structured dict asserting unsupported sheet status must be blocked
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    bad_json = json.dumps({"answer": {"sheet2": "empty", "note": "Sheet2 is empty"}, "citations": [c1.id]})
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
        patch("services.langgraph_pipeline.check_fidelity", return_value=True),
        patch("services.llm.llm_service.generate_completion", return_value=(bad_json, {"input_tokens": 10, "output_tokens": 10})),
    ):
        res_bad = qs.execute_query(req)

    assert res_bad["citations"] == []
