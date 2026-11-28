"""Offline audit tests for tabular claim verification and structured execution contracts.

Diagnosis-only suite (no production code modifications).
"""

from __future__ import annotations

from unittest.mock import patch

from db.table_store import get_shared_table_store
from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
from schemas.models import Chunk
from services.langgraph_pipeline import Pipeline


def _seed_registered_table(ws_id: str, doc_id: str, ver: str, *, complete: bool = True, extra_table: bool = False):
    from schemas.structured_table import (
        ColumnDefinition,
        HeaderTopology,
        InferredDtype,
        TableCell,
        TableRow,
        TableSchema,
    )

    store = get_shared_table_store(reset=True)
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Audit WS", "usr_audit", workspace_id=ws_id)
    b_repo.create_source(ws_id, "MO_Post_2024_25.csv", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="MO_Post_2024_25.csv",
            version=ver,
            format="csv",
            persisted_rows=4 if complete else 3,
            total_rows=4,
            incomplete_coverage_flag=not complete,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    table_id = "mo_post_2024_25"
    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="sl_no", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=1, name="system", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=2, name="sanction", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=3, name="filled", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=4, name="vacant", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=5, name="year", inferred_dtype=InferredDtype.INTEGER),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    raw_rows = [
        (1, "AYURVEDA", 685, 521, 164, 2024),
        (2, "UNANI", 105, 59, 46, 2024),
        (3, "HOMEOPATHY", 81, 47, 34, 2024),
        (4, "NATURE CURE & YOGA", 30, 19, 11, 2024),
    ]
    t_rows = [
        TableRow(
            row_id=f"row{i + 2}",
            row_index=i + 1,
            cells=tuple(
                TableCell(
                    cell_id=f"c_{i}_{j}",
                    row_index=i + 1,
                    col_index=j,
                    coordinate=f"{chr(65 + j)}{i + 2}",
                    raw_value=val,
                    normalized_value=str(val) if isinstance(val, str) else val,
                    inferred_dtype=InferredDtype.STRING if isinstance(val, str) else InferredDtype.INTEGER,
                )
                for j, val in enumerate(vals)
            ),
        )
        for i, vals in enumerate(raw_rows[: (4 if complete else 3)])
    ]

    def _reg(tid: str, d_id: str, s_obj: TableSchema):
        store.store_schema(tid, ws_id, s_obj, d_id, ver)
        store.write_row_batch_with_checkpoint(
            tid, ws_id, d_id, ver, t_rows,
            checkpoint_batch=1, source_rows=4, attempted_rows=len(t_rows),
            persisted_rows=len(t_rows), rejected_rows=0 if complete else 1,
            parser_version="1.0.0", schema_version="v1", content_hash="sha256:test",
        )
        store.publish_active_version(
            tid, ws_id, d_id, ver,
            manifest={
                "source_rows": 4,
                "persisted_rows": len(t_rows),
                "rejected_rows": 0 if complete else 1,
                "incomplete_coverage_flag": not complete,
                "coverage_complete": complete,
            },
        )

    _reg(table_id, doc_id, schema)
    if extra_table:
        _reg("mo_post_2023_24", f"{doc_id}_2", schema)
    return store, b_repo


class _MemRepo:
    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks

    def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
        return [c for c in self._chunks if not workspace_id or c.workspace_id == workspace_id]

    def search_vector(self, *a, **kw):
        return [(c, 0.9) for c in self._chunks]

    def search_vector_fast(self, *a, **kw):
        return [(c, 0.9) for c in self._chunks]

    def get(self, doc_id: str):
        return None


def test_audit_1_exact_tabular_lookup_on_registered_table():
    """Fully bound lookup on a ready table returns exact value and active citation with 0 generation calls (even with OKF off)."""
    from config import settings

    ws_id = "ws_audit_1"
    doc_id = "doc_audit_1"
    ver = "v_audit_1"
    store, b_repo = _seed_registered_table(ws_id, doc_id, ver, complete=True)
    chunks = [
        Chunk(
            id=f"{doc_id}:csv:row5", document_id=doc_id, source_format="csv",
            text="Sl. No.: 4. SYSTEM: NATURE CURE & YOGA. SANCTION: 30. FILLED: 19. VACANT: 11.",
            element_type="table_row", workspace_id=ws_id, version=ver, document_version=ver,
        ),
    ]
    repo = _MemRepo(chunks)
    p = Pipeline()
    with (
        patch.object(settings, "OKF_ENABLED", False),
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b_repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "titan")),
    ):
        res = p.run(
            query="How many medical officer posts are vacant in NATURE CURE & YOGA?",
            workspace_id=ws_id,
            boundary_repo=b_repo,
            laya_enabled=False,
            require_delivery_gate=False,
        )
    assert res.executed_path == "structured_aggregate"
    assert res.synthesis_calls == 0
    assert "11" in res.answer
    assert len(res.citations) == 1
    assert res.citations[0]["document_id"] == doc_id
    assert res.citations[0]["document_version"] == ver
    assert getattr(res, "terminal_state", None) == "COMPLETE"


def test_audit_2_same_entity_field_swap_and_wrong_year_or_unit():
    """Audit same-entity swapped fields and wrong year/unit with real planner, executor, and verifier."""
    ws_id = "ws_audit_2"
    doc_id = "doc_audit_2"
    ver = "v_audit_2"
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Audit WS 2", "usr_audit", workspace_id=ws_id)
    b_repo.create_source(ws_id, "audit.csv", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id, document_id=doc_id, source="audit.csv",
            version=ver, format="csv", persisted_rows=2, total_rows=2,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    chunks = [
        Chunk(
            id=f"{doc_id}:csv:row5", document_id=doc_id, source_format="csv",
            text="Sl. No.: 4. SYSTEM: NATURE CURE & YOGA. SANCTION: 30. FILLED: 19. VACANT: 11.",
            element_type="table_row", workspace_id=ws_id, version=ver, document_version=ver,
        ),
        Chunk(
            id=f"{doc_id}:csv:row_fin", document_id=doc_id, source_format="csv",
            text="ENTITY: AlphaCorp. YEAR: 2024. UNIT: USD_million. REVENUE: 184.2.",
            element_type="table_row", workspace_id=ws_id, version=ver, document_version=ver,
        ),
    ]
    repo = _MemRepo(chunks)
    p = Pipeline()
    get_shared_table_store(reset=True)

    def _mock_rerank(q, chs, top_k=6, return_mode=False):
        top = list(chs)[:top_k]
        return (top, "mocked") if return_mode else top

    scenarios = [
        (
            "supported",
            "How many medical officer posts are vacant in NATURE CURE & YOGA?",
            "There are 11 vacant medical officer posts in NATURE CURE & YOGA.",
            [f"{doc_id}:csv:row5"],
        ),
        (
            "supported_multi_field",
            "What are the filled and vacant medical officer posts for NATURE CURE & YOGA?",
            "NATURE CURE & YOGA has 19 filled posts and 11 vacant posts.",
            [f"{doc_id}:csv:row5"],
        ),
        (
            "same_entity_swapped_fields",
            "What are the filled and vacant medical officer posts for NATURE CURE & YOGA?",
            "NATURE CURE & YOGA has 11 filled posts and 19 vacant posts.",
            [f"{doc_id}:csv:row5"],
        ),
        (
            "supported_unit",
            "What is the 2024 revenue in USD million for AlphaCorp?",
            "AlphaCorp revenue in 2024 was 184.2 USD million.",
            [f"{doc_id}:csv:row_fin"],
        ),
        (
            "wrong_year",
            "What is the 2024 revenue in USD million for AlphaCorp?",
            "AlphaCorp revenue in 2025 was 184.2 USD million.",
            [f"{doc_id}:csv:row_fin"],
        ),
        (
            "wrong_currency",
            "What is the 2024 revenue in USD million for AlphaCorp?",
            "AlphaCorp revenue in 2024 was 184.2 EUR million.",
            [f"{doc_id}:csv:row_fin"],
        ),
        (
            "wrong_scale",
            "What is the 2024 revenue in USD million for AlphaCorp?",
            "AlphaCorp revenue in 2024 was 184.2 USD billion.",
            [f"{doc_id}:csv:row_fin"],
        ),
    ]
    outcomes = {}
    for label, q_text, draft, cids in scenarios:
        with (
            patch("services.langgraph_pipeline._get_repo", return_value=repo),
            patch("services.langgraph_pipeline.rerank", side_effect=_mock_rerank),
            patch("services.langgraph_pipeline.call_llm", return_value={"answer": draft, "citations": cids, "usage": {"input_tokens": 50, "output_tokens": 15}}),
            patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
            patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "titan")),
        ):
            res = p.run(query=q_text, workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
            outcomes[label] = (res.answer, [c.get("chunk_id") for c in res.citations], res.synthesis_calls)
            print(f"AUDIT_2[{label}]: ans={res.answer!r} cits={outcomes[label][1]} synth={res.synthesis_calls}")

    assert "11" in outcomes["supported"][0]
    assert outcomes["supported"][1] == [f"{doc_id}:csv:row5"]

    assert "19" in outcomes["supported_multi_field"][0] and "11" in outcomes["supported_multi_field"][0]
    assert outcomes["supported_multi_field"][1] == [f"{doc_id}:csv:row5"]

    assert outcomes["same_entity_swapped_fields"][0] == "NOT_FOUND"
    assert outcomes["same_entity_swapped_fields"][1] == []

    assert "184.2" in outcomes["supported_unit"][0]
    assert outcomes["supported_unit"][1] == [f"{doc_id}:csv:row_fin"]

    assert outcomes["wrong_year"][0] == "NOT_FOUND"
    assert outcomes["wrong_year"][1] == []

    assert outcomes["wrong_currency"][0] == "NOT_FOUND"
    assert outcomes["wrong_currency"][1] == []

    assert outcomes["wrong_scale"][0] == "NOT_FOUND"
    assert outcomes["wrong_scale"][1] == []


def test_audit_3_terminal_states_no_match_incomplete_and_ambiguous():
    """Audit machine-readable terminal states for bound no-match, unbound filter, empty COUNT=0, incomplete snapshot, and ambiguous selection."""
    from services.retrieval.run_structured_aggregate import execute

    # 1. Complete, bound year=2026 lookup with zero matching rows -> NO_SUPPORT, zero citations
    ws1 = "ws_term_nomatch"
    _, b1 = _seed_registered_table(ws1, "doc_1", "v1", complete=True)
    with patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b1):
        r_bound_empty = execute({"workspace_id": ws1, "query": "What is the 2026 vacant posts for AYURVEDA in mo_post_2024_25.csv?", "stage_timings": {}})
    assert r_bound_empty.get("executed_path") == "structured_aggregate"
    assert r_bound_empty.get("terminal_state") == "NO_SUPPORT"
    assert (r_bound_empty.get("citations") or []) == []

    # 1a. Unbound filter keeps its own unresolved_filter outcome
    with patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b1):
        r_unbound = execute({"workspace_id": ws1, "query": "How many medical officer posts are vacant in SIDDHA?", "stage_timings": {}})
    assert r_unbound.get("executed_path") == "structured_aggregate_unresolved_filter"
    assert r_unbound.get("error_code") == "unresolved_filter"
    assert r_unbound.get("terminal_state") == "FAILED"
    assert (r_unbound.get("citations") or []) == []

    # 1b. Empty COUNT = 0 preserved as COMPLETE with 0
    with patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b1):
        r_count0 = execute({"workspace_id": ws1, "query": "What is the count of records for 2026?", "stage_timings": {}})
    assert r_count0.get("executed_path") == "structured_aggregate"
    assert r_count0.get("terminal_state") == "COMPLETE"
    assert "0" in str(r_count0.get("final_answer"))

    # 2. Incomplete snapshot -> RETRIEVAL_INCOMPLETE
    ws2 = "ws_term_inc"
    _, b2 = _seed_registered_table(ws2, "doc_2", "v1", complete=False)
    with patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b2):
        r_inc = execute({"workspace_id": ws2, "query": "How many medical officer posts are vacant in AYURVEDA?", "stage_timings": {}})
    assert r_inc.get("executed_path") == "structured_aggregate_incomplete"
    assert r_inc.get("terminal_state") == "RETRIEVAL_INCOMPLETE"

    # 3. Ambiguous selection -> CLARIFICATION_REQUIRED
    ws3 = "ws_term_ambig"
    _, b3 = _seed_registered_table(ws3, "doc_3", "v1", complete=True, extra_table=True)
    with patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b3):
        r_ambig = execute({"workspace_id": ws3, "query": "How many medical officer posts are vacant in AYURVEDA?", "stage_timings": {}})
    assert r_ambig.get("executed_path") == "structured_aggregate_ambiguous"
    assert r_ambig.get("terminal_state") == "CLARIFICATION_REQUIRED"


def test_audit_4_generic_table_cardinality_and_unit_controls():
    """Verify single-row lookup (field+unit), multi-year ambiguity, explicit sum, and bound empty NO_SUPPORT through Pipeline."""
    from decimal import Decimal
    from schemas.structured_table import ColumnDefinition, HeaderTopology, InferredDtype, TableCell, TableRow, TableSchema

    ws_id = "ws_generic_card"
    doc_id = "doc_fin"
    ver = "v_fin_1"
    store = get_shared_table_store(reset=True)
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Fin WS", "usr_fin", workspace_id=ws_id)
    b_repo.create_source(ws_id, "financials.csv", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="financials.csv",
            version=ver,
            format="csv",
            persisted_rows=2,
            total_rows=2,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    schema = TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="entity", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=1, name="year", inferred_dtype=InferredDtype.INTEGER),
            ColumnDefinition(col_index=2, name="revenue", inferred_dtype=InferredDtype.DECIMAL, unit="USD_million"),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
    )
    t_rows = [
        TableRow(
            row_id="r_2023", row_index=0,
            cells=(
                TableCell(cell_id="c00", row_index=0, col_index=0, coordinate="A2", raw_value="AlphaCorp", normalized_value="AlphaCorp"),
                TableCell(cell_id="c01", row_index=0, col_index=1, coordinate="B2", raw_value="2023", normalized_value=2023, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c02", row_index=0, col_index=2, coordinate="C2", raw_value="120.0", normalized_value=Decimal("120.0"), inferred_dtype=InferredDtype.DECIMAL, unit="USD_million"),
            ),
        ),
        TableRow(
            row_id="r_2024", row_index=1,
            cells=(
                TableCell(cell_id="c10", row_index=1, col_index=0, coordinate="A3", raw_value="AlphaCorp", normalized_value="AlphaCorp"),
                TableCell(cell_id="c11", row_index=1, col_index=1, coordinate="B3", raw_value="2024", normalized_value=2024, inferred_dtype=InferredDtype.INTEGER),
                TableCell(cell_id="c12", row_index=1, col_index=2, coordinate="C3", raw_value="184.2", normalized_value=Decimal("184.2"), inferred_dtype=InferredDtype.DECIMAL, unit="USD_million"),
            ),
        ),
    ]
    store.store_schema("financials", ws_id, schema, doc_id, ver)
    store.write_row_batch_with_checkpoint(
        "financials", ws_id, doc_id, ver, t_rows,
        checkpoint_batch=1, source_rows=2, attempted_rows=2, persisted_rows=2, rejected_rows=0,
        parser_version="1.0.0", schema_version="v1", content_hash="sha256:fin",
    )
    store.publish_active_version(
        "financials", ws_id, doc_id, ver,
        manifest={"source_rows": 2, "persisted_rows": 2, "rejected_rows": 0, "incomplete_coverage_flag": False, "coverage_complete": True},
    )

    repo = _MemRepo([])
    p = Pipeline()
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b_repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "titan")),
    ):
        # 1. Explicit year -> exact single-row lookup with field, unit, active row citation, 0 gen calls
        r1 = p.run(query="What is the 2024 revenue for AlphaCorp in financials.csv?", workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
        assert r1.executed_path == "structured_aggregate"
        assert r1.terminal_state == "COMPLETE"
        assert r1.synthesis_calls == 0
        assert "184.2" in r1.answer and "USD_million" in r1.answer
        assert r1.citations[0]["operator"] == "lookup"
        assert r1.citations[0]["row_ids"] == ["r_2024"]
        assert r1.citations[0]["document_version"] == ver
        assert r1.citations[0]["unit"] == "USD_million"

        # 2. Omitted year for single-value request -> CLARIFICATION_REQUIRED (0 gen calls)
        r2 = p.run(query="What is the revenue for AlphaCorp in financials.csv?", workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
        assert r2.executed_path == "structured_aggregate_ambiguous"
        assert r2.terminal_state == "CLARIFICATION_REQUIRED"
        assert r2.synthesis_calls == 0
        assert r2.citations == []

        # 3. Explicit sum across both years -> sums 120.0 + 184.2 = 304.2
        r3 = p.run(query="What is the total revenue for AlphaCorp across 2023 and 2024 in financials.csv?", workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
        assert r3.executed_path == "structured_aggregate"
        assert r3.terminal_state == "COMPLETE"
        assert r3.synthesis_calls == 0
        assert "304.2" in r3.answer and r3.citations[0]["operator"] == "sum" and r3.citations[0]["selection_count"] == 2

        # 4. Complete bound empty selection (2026) -> NO_SUPPORT, zero citations
        r4 = p.run(query="What is the 2026 revenue for AlphaCorp in financials.csv?", workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
        assert r4.executed_path == "structured_aggregate"
        assert r4.terminal_state == "NO_SUPPORT"
        assert r4.synthesis_calls == 0
        assert r4.citations == []

        # 5. Empty COUNT -> 0, COMPLETE
        r5 = p.run(query="What is the count of records for 2026 in financials.csv?", workspace_id=ws_id, boundary_repo=b_repo, laya_enabled=False, require_delivery_gate=False)
        assert r5.executed_path == "structured_aggregate"
        assert r5.terminal_state == "COMPLETE"
        assert "0" in r5.answer
