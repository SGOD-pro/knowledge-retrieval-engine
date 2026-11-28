"""Offline reproduction of Q168 distractor citation pass-through and Q155 row lineage check."""

import json
from pathlib import Path
from unittest.mock import patch

from db.table_store.memory_store import MemoryTableStore
from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
from ingestion.csv_table_ingester import ingest_csv_to_table_store
from schemas.models import Chunk
from services.langgraph_pipeline import pipeline
from services.retrieval import bm25_retriever


class _PaperStubRepo:
    def __init__(self, chunks: list[Chunk]):
        self._chunks = chunks

    def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
        return [c for c in self._chunks if not workspace_id or c.workspace_id == workspace_id]

    def search_vector(self, *args, **kwargs):
        return [(c, 0.9) for c in self._chunks]

    def search_vector_fast(self, *args, **kwargs):
        return [(c, 0.9) for c in self._chunks]

    def get(self, doc_id: str):
        return None


def _run_paper_query(answer_text: str, cited_id: str, custom_chunks: list[Chunk] | None = None):
    ws_id = "ws_q168_repro"
    doc_id = "doc_paper"
    c_support = Chunk(
        id=f"{doc_id}:page:2:element:1",
        document_id=doc_id,
        source_format="pdf",
        page_number=2,
        text="The Focus-Classify Attention Model (FCAM) combines a focus network with a classification network.",
        element_type="paragraph",
        workspace_id=ws_id,
        version="ver_paper_01",
        document_version="ver_paper_01",
    )
    c_distractor = Chunk(
        id=f"{doc_id}:page:6:element:5",
        document_id=doc_id,
        source_format="pdf",
        page_number=6,
        text="More concretely, a FCAM routes the highest-scoring segment to the classifier.",
        element_type="paragraph",
        workspace_id=ws_id,
        version="ver_paper_01",
        document_version="ver_paper_01",
    )
    chunks = custom_chunks if custom_chunks is not None else [c_distractor, c_support]
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Paper WS", "usr_alice", workspace_id=ws_id)
    b_repo.create_source(ws_id, "interpretability_paper.pdf", source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source="interpretability_paper.pdf",
            version="ver_paper_01",
            format="pdf",
            persisted_rows=len(chunks),
            total_rows=len(chunks),
            created_at="2026-11-28T00:00:00Z",
        )
    )
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    repo = _PaperStubRepo(chunks)
    llm_payload = json.dumps({"answer": answer_text, "citations": [cited_id]})
    with (
        patch("services.langgraph_pipeline._get_repo", return_value=repo),
        patch("providers.embedding_provider.embed_text", return_value=[0.1] * 1024),
        patch("providers.embedding_provider.embed_text_with_provenance", return_value=([0.1] * 1024, "amazon.titan-embed-text-v2:0")),
        patch("services.llm.llm_service.generate_completion", return_value=(llm_payload, {"input_tokens": 25, "output_tokens": 12})) as gen_mock,
    ):
        res = pipeline.run(
            query="What is the expanded name of FCAM in the interpretability paper?",
            document_ids=[doc_id],
            workspace_id=ws_id,
            benchmark_mode=True,
            boundary_repo=b_repo,
            semantic_verifier=None,
        )
    return res, gen_mock.call_count


def test_q168_wrong_expansion_and_wrong_citation_recovers_unique_explicit_definition():
    res, gen_calls = _run_paper_query(
        "Fully Convolutional Attention Mechanism",
        "doc_paper:page:6:element:5",
    )
    assert gen_calls == 1
    assert res.executed_path == "full"
    assert "Fully Convolutional Attention Mechanism" not in res.answer
    assert "Focus-Classify Attention Model" in res.answer
    assert [c["chunk_id"] for c in res.citations] == ["doc_paper:page:2:element:1"]
    assert res.citations[0]["document_version"] == "ver_paper_01"


def test_q168_correct_expansion_and_wrong_citation_rebinds_to_supporting_chunk():
    res, gen_calls = _run_paper_query(
        "Focus-Classify Attention Model",
        "doc_paper:page:6:element:5",
    )
    assert gen_calls == 1
    assert "Focus-Classify Attention Model" in res.answer
    assert [c["chunk_id"] for c in res.citations] == ["doc_paper:page:2:element:1"]
    assert res.citations[0]["document_version"] == "ver_paper_01"


def test_q168_bare_acronym_mention_only_declines_expansion_with_zero_citations():
    bare_chunk = Chunk(
        id="doc_paper:page:6:element:5",
        document_id="doc_paper",
        source_format="pdf",
        page_number=6,
        text="More concretely, a FCAM routes the highest-scoring segment to the classifier.",
        element_type="paragraph",
        workspace_id="ws_q168_repro",
        version="ver_paper_01",
        document_version="ver_paper_01",
    )
    res, gen_calls = _run_paper_query(
        "Fully Convolutional Attention Mechanism",
        "doc_paper:page:6:element:5",
        custom_chunks=[bare_chunk],
    )
    assert gen_calls == 1
    assert "Fully Convolutional Attention Mechanism" not in res.answer
    assert res.citations == []


def test_q168_conflicting_definitions_qualifies_instead_of_emitting_single_claim():
    c1 = Chunk(
        id="doc_paper:page:2:element:1",
        document_id="doc_paper",
        source_format="pdf",
        page_number=2,
        text="The Focus-Classify Attention Model (FCAM) combines two networks.",
        element_type="paragraph",
        workspace_id="ws_q168_repro",
        version="ver_paper_01",
        document_version="ver_paper_01",
    )
    c2 = Chunk(
        id="doc_paper:page:9:element:1",
        document_id="doc_paper",
        source_format="pdf",
        page_number=9,
        text="The Fast Context Aggregation Module (FCAM) pools regional features.",
        element_type="paragraph",
        workspace_id="ws_q168_repro",
        version="ver_paper_01",
        document_version="ver_paper_01",
    )
    res, gen_calls = _run_paper_query(
        "Focus-Classify Attention Model",
        "doc_paper:page:2:element:1",
        custom_chunks=[c1, c2],
    )
    assert gen_calls == 1
    assert "conflicting" in res.answer.lower()
    assert "Focus-Classify Attention Model" in res.answer
    assert "Fast Context Aggregation Module" in res.answer


def test_q168_positive_control_preserves_supporting_active_version_citation():
    res, gen_calls = _run_paper_query(
        "Focus-Classify Attention Model",
        "doc_paper:page:2:element:1",
    )
    assert gen_calls == 1
    assert res.executed_path == "full"
    assert "Focus-Classify Attention Model" in res.answer
    assert [c["chunk_id"] for c in res.citations] == ["doc_paper:page:2:element:1"]
    assert res.citations[0]["document_version"] == "ver_paper_01"


def test_q155_lineage_tracks_all_three_rows_in_retained_evidence_and_candidates(tmp_path: Path):
    csv_p = tmp_path / "synth_ledger.csv"
    csv_p.write_text(
        "Year,Industry,Variable,Value\n"
        "2025,All industries,Total income,979702\n"
        "2025,All industries,Total expenditure,871375\n"
        "2025,All industries,Surplus before income tax,109848\n",
        encoding="utf-8",
    )
    ws_id = "ws_q155_lineage"
    doc_id = "doc_ledger"
    t_store = MemoryTableStore()
    manifest = ingest_csv_to_table_store(csv_p, document_id=doc_id, workspace_id=ws_id, store=t_store)
    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Ledger WS", "usr_alice", workspace_id=ws_id)
    b_repo.create_source(ws_id, csv_p.name, source_id=doc_id)
    b_repo.save_document_snapshot(
        DocumentSnapshot(
            workspace_id=ws_id,
            document_id=doc_id,
            source=csv_p.name,
            version=manifest.document_version,
            format="csv",
            persisted_rows=3,
            total_rows=3,
            created_at="2026-11-28T00:00:00Z",
        )
    )
    q = "For 2025 All industries, does total income minus total expenditure equal surplus before income tax in synth_ledger.csv?"
    with (
        patch("db.table_store.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_shared_table_store", return_value=t_store),
        patch("services.retrieval.run_structured_aggregate.get_workspace_boundary_repo", return_value=b_repo),
    ):
        res = pipeline.run(query=q, document_ids=[doc_id], workspace_id=ws_id, benchmark_mode=True, boundary_repo=b_repo)

    assert res.citations[0]["selection_count"] == 3
    assert "row_ids" not in res.citations[0]
    assert len(res.retained_evidence_items[0].locator["row_ids"]) == 3
    assert len(res.retrieval_candidates["structured_rows"]) == 3
