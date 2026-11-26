from services.langgraph_pipeline import pipeline


def test_citation_provenance_invariant(monkeypatch):
    """Invariant test verifying that every citation in response.citations
    strictly originates from a retrieved chunk in response.top_chunks.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    query = "How many parallel attention heads h are employed in the Multi-Head Attention mechanism of the base Transformer model?"

    from db.database import CloudRepository
    from schemas.models import Document, Chunk
    from ingestion.embed_service import embed_fast_local, _deterministic_vector

    repo = CloudRepository()
    doc = Document(
        id="transformer_doc_1",
        filename="transformer_paper.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="transformer_doc_1:c1",
                document_id="transformer_doc_1",
                text="In the base Transformer model, h = 8 parallel attention layers or heads are employed.",
                source_format="pdf",
                page_number=4,
                element_type="paragraph",
                section_path=("3.2.2 Multi-Head Attention",),
                embedding_fast=embed_fast_local(query),
                embedding_full=_deterministic_vector(query, 1024),
                workspace_id="ws_test",
            ),
        ),
        workspace_id="ws_test",
    )
    repo.save(doc)

    # Run the pipeline
    response = pipeline.run(query, workspace_id="ws_test")

    citations = response.citations
    assert len(citations) > 0, "No citations returned by pipeline"

    top_chunk_ids = {c.id for c in response.top_chunks}

    for i, c in enumerate(citations):
        assert isinstance(
            c, dict
        ), f"Citation {i} is not a dictionary! Found: {type(c)}"
        assert "chunk_id" in c, f"Citation {i} is missing chunk_id! Content: {c}"

        cid = c["chunk_id"]
        assert (
            cid in top_chunk_ids
        ), f"PROVENANCE FAILURE! Citation chunk_id '{cid}' was NOT in top_chunks!"


def test_q162_unsupported_citation_fallback_and_controls(monkeypatch):
    """Q162 regression: empty/invalid model citations must cite only supporting evidence or refuse."""
    import json
    from unittest.mock import patch
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.telemetry import init_request_telemetry

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws = "ws_q162_fallback"

    c_bill = Chunk(
        id="doc-bills:csv:row4",
        document_id="doc-bills",
        source_format="csv",
        text="Year: 2014. Bill No.: LIII. Short Title of the Bill: THE ANTI-HIJACKING BILL, 2016. Referred to Committee / Report Presented: . Status: Assented.",
        element_type="table_row",
        page_number=None,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_ai = Chunk(
        id="doc-ai:page:15:element:12",
        document_id="doc-ai",
        source_format="pdf",
        text="National Strategy for Artificial Intelligence discusses machine learning research and healthcare.",
        element_type="text",
        page_number=15,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_climate = Chunk(
        id="doc-climate:page:3:element:1",
        document_id="doc-climate",
        source_format="pdf",
        text="Renewable energy targets for 2030 include solar capacity expansion across coastal states.",
        element_type="text",
        page_number=3,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    class _FallbackDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {
                "documents": [
                    {"id": "doc-bills", "filename": "bills.csv"},
                    {"id": "doc-ai", "filename": "ai_strategy.pdf"},
                    {"id": "doc-climate", "filename": "climate.pdf"},
                ]
            }

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return [c_bill, c_ai, c_climate]

        def get(self, doc_id: str):
            return None

        def search_vector(self, *args, **kwargs):
            return []

        def check_semantic_cache(self, *args, **kwargs):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Q162 WS", "owner-1", workspace_id=ws)
    for d_id, fname, fmt in (
        ("doc-bills", "bills.csv", "csv"),
        ("doc-ai", "ai_strategy.pdf", "pdf"),
        ("doc-climate", "climate.pdf", "pdf"),
    ):
        b_repo.create_source(ws, fname, source_id=d_id)
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws,
                document_id=d_id,
                source=fname,
                version="v1",
                format=fmt,
                created_at="2026-11-26T00:00:00Z",
            )
        )

    doc_repo = _FallbackDocRepo()
    qs = QueryService(repo=doc_repo)

    def _run_with_llm(query_text: str, raw_llm_json: str):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        init_request_telemetry()
        req = QueryRequest(
            query=query_text,
            workspace_id=ws,
            benchmark_mode=True,
            cache=False,
            force_full_path=True,
        )
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(raw_llm_json, {"input_tokens": 10, "output_tokens": 10})) as mock_gen,
        ):
            out = qs.execute_query(req)
            assert mock_gen.call_count == 1
            return out

    q162_query = "Does a blank Committee / Report Presented cell for the Anti-Hijacking Bill prove that it was never referred to a committee?"
    q162_ans = "No, a blank Committee / Report Presented cell for the Anti-Hijacking Bill does not prove it was never referred to a committee."

    # 1. Empty model citations [] -> must cite only c_bill, never c_ai or c_climate
    res_empty_cits = _run_with_llm(q162_query, json.dumps({"answer": q162_ans, "citations": []}))
    assert [c["chunk_id"] for c in res_empty_cits["citations"]] == [c_bill.id]
    assert res_empty_cits["generation_calls"] <= 1

    # 2. Invalid model citation IDs -> must cite only c_bill
    res_invalid_cits = _run_with_llm(q162_query, json.dumps({"answer": q162_ans, "citations": ["unknown-chunk:99"]}))
    assert [c["chunk_id"] for c in res_invalid_cits["citations"]] == [c_bill.id]

    # 3. Empty raw model answer -> scoped refusal, no fallback citations
    res_empty_ans = _run_with_llm(q162_query, json.dumps({"answer": "", "citations": []}))
    assert res_empty_ans["citations"] == []
    assert res_empty_ans["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_empty_ans["answer"].lower()

    # 4. Mixed valid + unknown citation IDs -> preserves valid citation only
    res_mixed = _run_with_llm(q162_query, json.dumps({"answer": q162_ans, "citations": [c_bill.id, "nonexistent-id"]}))
    assert [c["chunk_id"] for c in res_mixed["citations"]] == [c_bill.id]

    # 5. Multi-claim answer with missing support + empty citations -> scoped refusal
    multi_unsupported = (
        "The Anti-Hijacking Bill 2016 has a blank Committee / Report Presented cell. "
        "Mars atmospheric pressure averages 610 pascals in winter."
    )
    res_missing_claim = _run_with_llm(q162_query, json.dumps({"answer": multi_unsupported, "citations": []}))
    assert res_missing_claim["citations"] == []
    assert res_missing_claim["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_missing_claim["answer"].lower()

    # 6. Legitimate multi-document answer with empty citations -> cites both supporting docs, excludes c_climate
    multi_doc_ans = (
        "The Anti-Hijacking Bill 2016 is assented with a blank Committee / Report Presented cell. "
        "Meanwhile, the National Strategy for Artificial Intelligence discusses machine learning research."
    )
    res_multi_doc = _run_with_llm(
        "Summarize the Anti-Hijacking Bill status and the National Strategy for Artificial Intelligence topic.",
        json.dumps({"answer": multi_doc_ans, "citations": []}),
    )
    cited_ids = {c["chunk_id"] for c in res_multi_doc["citations"]}
    assert cited_ids == {c_bill.id, c_ai.id}
    assert res_multi_doc["generation_calls"] <= 1

    # 7. Contradiction despite high lexical overlap -> fallback must refuse
    res_contradict = _run_with_llm(
        "Was the Anti-Hijacking Bill referred to a committee in 2014?",
        json.dumps({"answer": "Yes, the Anti-Hijacking Bill was referred to a committee in 2014.", "citations": []}),
    )
    # Wait: c_bill has blank Committee cell; test explicit negation in a dedicated contradicting chunk
    c_contra = Chunk(
        id="doc-contra:page:1:element:0",
        document_id="doc-ai",
        source_format="pdf",
        text="AlphaSystem is not deployed in healthcare clinics during 2025.",
        element_type="text",
        page_number=1,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    doc_repo_contra = _FallbackDocRepo()
    doc_repo_contra.get_all_chunks = lambda document_ids=None, workspace_id="": [c_contra]
    qs_contra = QueryService(repo=doc_repo_contra)
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    init_request_telemetry()
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo_contra),
        patch("services.langgraph_pipeline.check_fidelity", return_value=True),
        patch(
            "services.llm.llm_service.generate_completion",
            return_value=(json.dumps({"answer": "AlphaSystem is deployed in healthcare clinics during 2025.", "citations": []}), {"input_tokens": 10, "output_tokens": 10}),
        ),
    ):
        res_contra = qs_contra.execute_query(
            QueryRequest(query="Is AlphaSystem deployed in healthcare clinics during 2025?", workspace_id=ws, benchmark_mode=True, cache=False, force_full_path=True)
        )
    assert res_contra["citations"] == []
    assert res_contra["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_contra["answer"].lower()

    # 8. Same number belonging to a different entity or period -> fallback must refuse
    c_table_period = Chunk(
        id="doc-tbl:page:2:element:0",
        document_id="doc-ai",
        source_format="pdf",
        text="Condensed Operations: Products revenue 500 million (2024). Services revenue 300 million (2025).",
        element_type="text",
        page_number=2,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    doc_repo_period = _FallbackDocRepo()
    doc_repo_period.get_all_chunks = lambda document_ids=None, workspace_id="": [c_table_period]
    qs_period = QueryService(repo=doc_repo_period)
    if hasattr(bm25_retriever, "_CHUNK_CACHE"):
        bm25_retriever._CHUNK_CACHE.clear()
    init_request_telemetry()
    with (
        patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
        patch("services.langgraph_pipeline._get_repo", return_value=doc_repo_period),
        patch("services.langgraph_pipeline.check_fidelity", return_value=True),
        patch(
            "services.llm.llm_service.generate_completion",
            return_value=(json.dumps({"answer": "2025 Services revenue was 500 million.", "citations": []}), {"input_tokens": 10, "output_tokens": 10}),
        ),
    ):
        res_wrong_period = qs_period.execute_query(
            QueryRequest(query="What was Services revenue in 2025?", workspace_id=ws, benchmark_mode=True, cache=False, force_full_path=True)
        )
    assert res_wrong_period["citations"] == []
    assert res_wrong_period["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_wrong_period["answer"].lower()

    # 9. Explicit valid IDs (genuinely supporting + real retained but unrelated) bypass fallback checks
    res_explicit_mixed = _run_with_llm(q162_query, json.dumps({"answer": q162_ans, "citations": [c_bill.id, c_ai.id]}))
    assert [c["chunk_id"] for c in res_explicit_mixed["citations"]] == [c_bill.id, c_ai.id]


def test_opt_in_lexical_rrf_blend_through_query_service(monkeypatch):
    """Opt-in LEXICAL_RRF_BLEND_ENABLED changes ranking in QueryService while default preserves plain BM25Plus."""
    import json
    from unittest.mock import patch
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.telemetry import init_request_telemetry

    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws = "ws_rrf_blend_opt_in"
    query_text = "What unit and value are recorded for the 2025 Current ratio?"

    # Supporting chunk has strong lexical overlap + RRF rank 0 from vector search
    c_target = Chunk(
        id="doc-tbl:csv:row28",
        document_id="doc-tbl",
        source_format="csv",
        text="Year: 2025. Unit and value recorded for 2025 current ratio: 1.42.",
        element_type="table_row",
        page_number=None,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    # 8 distractors across two competing PDFs repeat "current ratio" twice so plain BM25Plus drops c_target
    distractors = [
        Chunk(
            id=f"doc-pdf-{'a' if i < 4 else 'b'}:page:{i+1}:element:0",
            document_id=f"doc-pdf-{'a' if i < 4 else 'b'}",
            source_format="pdf",
            text=f"Section {i+1}: unit and value recorded for 2025 current ratio current ratio.",
            element_type="text",
            page_number=i + 1,
            workspace_id=ws,
            version="v1",
            document_version="v1",
        )
        for i in range(8)
    ]
    pool_chunks = [c_target] + distractors

    class _BlendDocRepo:
        def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
            return {
                "documents": [
                    {"id": "doc-tbl", "filename": "survey.csv"},
                    {"id": "doc-pdf-a", "filename": "notes_a.pdf"},
                    {"id": "doc-pdf-b", "filename": "notes_b.pdf"},
                ]
            }

        def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
            return pool_chunks

        def get(self, doc_id: str):
            return None

        def search_vector(self, *args, **kwargs):
            return [(c_target, 0.99)]

        def check_semantic_cache(self, *args, **kwargs):
            return None

    b_repo = InMemoryWorkspaceBoundaryRepository()
    b_repo.create_workspace("Blend WS", "owner-1", workspace_id=ws)
    for d_id, fname, fmt in (
        ("doc-tbl", "survey.csv", "csv"),
        ("doc-pdf-a", "notes_a.pdf", "pdf"),
        ("doc-pdf-b", "notes_b.pdf", "pdf"),
    ):
        b_repo.create_source(ws, fname, source_id=d_id)
        b_repo.save_document_snapshot(
            DocumentSnapshot(workspace_id=ws, document_id=d_id, source=fname, version="v1", format=fmt, created_at="2026-11-26T00:00:00Z")
        )

    doc_repo = _BlendDocRepo()
    qs = QueryService(repo=doc_repo)

    def _exec(flag_val: str | None):
        if flag_val is None:
            monkeypatch.delenv("LEXICAL_RRF_BLEND_ENABLED", raising=False)
        else:
            monkeypatch.setenv("LEXICAL_RRF_BLEND_ENABLED", flag_val)
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        init_request_telemetry()
        req = QueryRequest(query=query_text, workspace_id=ws, benchmark_mode=True, cache=False, force_full_path=True)
        llm_resp = json.dumps({"answer": "The 2025 liquidity ratio value is 1.42.", "citations": [c_target.id]})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(llm_resp, {"input_tokens": 10, "output_tokens": 10})),
        ):
            return qs.execute_query(req)

    # Default (flag off): plain BM25Plus policy, c_target dropped by distractors
    res_off = _exec(None)
    assert res_off["reranker_telemetry"].get("model") == "bm25plus"
    assert res_off["reranker_telemetry"].get("reranker_policy") is None
    assert c_target.id not in res_off["compressed_context_chunk_ids"]

    # Opt-in (flag on): experimental blend promotes c_target into compressed_context and citations
    res_on = _exec("true")
    assert res_on["reranker_telemetry"].get("model") == "bm25plus_rrf_blend"
    assert res_on["reranker_telemetry"].get("reranker_policy") == "lexical_bm25_plus_rrf_blend"
    assert c_target.id in res_on["compressed_context_chunk_ids"]
    assert [c["chunk_id"] for c in res_on["citations"]] == [c_target.id]


def test_partial_claim_coverage_in_citation_fallback(monkeypatch):
    """Fallback must cover all factual claims in a compound sentence or refuse."""
    import json
    from unittest.mock import patch
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.telemetry import init_request_telemetry

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws = "ws_partial_claim_fallback"

    c_turbine = Chunk(
        id="doc-turbine:page:1:element:0",
        document_id="doc-turbine",
        source_format="pdf",
        text="Northbridge Turbine relies on magnetic bearings for frictionless rotor suspension.",
        element_type="text",
        page_number=1,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_battery = Chunk(
        id="doc-battery:page:1:element:0",
        document_id="doc-battery",
        source_format="pdf",
        text="Southport Battery uses solid-state electrolyte cells for thermal stability.",
        element_type="text",
        page_number=1,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_distractor = Chunk(
        id="doc-distractor:page:1:element:0",
        document_id="doc-distractor",
        source_format="pdf",
        text=(
            "Legacy Turbine Maintenance Guide mentions magnetic bearings and thermal stability "
            "only in historical valve checklists."
        ),
        element_type="text",
        page_number=1,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    def _run_case(pool_chunks: list[Chunk], answer_text: str):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        if hasattr(bm25_retriever, "_BM25_CACHE"):
            bm25_retriever._BM25_CACHE.clear()
        init_request_telemetry()

        docs_meta = [
            (c.document_id, f"{c.document_id}.pdf", "pdf")
            for c in pool_chunks
        ]

        class _DocRepo:
            def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
                return {"documents": [{"id": d, "filename": fn} for d, fn, _ in docs_meta]}

            def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
                return list(pool_chunks)

            def get(self, doc_id: str):
                return None

            def search_vector(self, *args, **kwargs):
                return []

            def check_semantic_cache(self, *args, **kwargs):
                return None

        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace("Partial Claim WS", "tester", workspace_id=ws)
        for d_id, fname, fmt in docs_meta:
            b_repo.create_source(ws, fname, source_id=d_id)
            b_repo.save_document_snapshot(
                DocumentSnapshot(
                    workspace_id=ws,
                    document_id=d_id,
                    source=fname,
                    version="v1",
                    format=fmt,
                    created_at="2026-11-26T00:00:00Z",
                )
            )

        doc_repo = _DocRepo()
        qs = QueryService(repo=doc_repo)
        req = QueryRequest(
            query="How do Northbridge Turbine and Southport Battery operate?",
            workspace_id=ws,
            benchmark_mode=True,
            cache=False,
            force_full_path=True,
        )
        raw_json = json.dumps({"answer": answer_text, "citations": []})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(raw_json, {"input_tokens": 10, "output_tokens": 10})) as mock_gen,
        ):
            out = qs.execute_query(req)
            assert mock_gen.call_count <= 1
            return out

    compound_ans = (
        "The station pairs magnetic bearings in Northbridge Turbine "
        "and solid-state electrolyte cells in Southport Battery."
    )

    # 1. Both sources present -> must cite both c_turbine and c_battery (not distractor)
    res_both = _run_case([c_turbine, c_battery, c_distractor], compound_ans)
    cited_ids_both = {c["chunk_id"] for c in res_both["citations"]}
    assert cited_ids_both == {c_turbine.id, c_battery.id}
    assert c_distractor.id not in cited_ids_both

    # 2. Either source removed -> must refuse unsupported compound answer
    res_no_battery = _run_case([c_turbine, c_distractor], compound_ans)
    assert res_no_battery["citations"] == []
    assert res_no_battery["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_no_battery["answer"].lower()

    res_no_turbine = _run_case([c_battery, c_distractor], compound_ans)
    assert res_no_turbine["citations"] == []
    assert res_no_turbine["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_no_turbine["answer"].lower()

    # 3. Valid multi-document control (separate sentences) -> must still cite both
    multi_sent_ans = (
        "Northbridge Turbine relies on magnetic bearings for frictionless rotor suspension. "
        "Southport Battery uses solid-state electrolyte cells for thermal stability."
    )
    res_multi = _run_case([c_turbine, c_battery, c_distractor], multi_sent_ans)
    assert {c["chunk_id"] for c in res_multi["citations"]} == {c_turbine.id, c_battery.id}


def test_header_only_evidence_in_citation_fallback(monkeypatch):
    """Heading-only match over unrelated body must be refused; genuine factual captions pass."""
    import json
    from unittest.mock import patch
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.telemetry import init_request_telemetry

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws = "ws_header_only_fallback"

    c_supporting = Chunk(
        id="doc-grid:page:1:element:0",
        document_id="doc-grid",
        source_format="pdf",
        text=(
            "Coastal Microgrid Architecture\n"
            "The inverter array relies on galvanic isolation and synchronous rectification "
            "for fault ride-through."
        ),
        element_type="text",
        page_number=1,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_header_only = Chunk(
        id="doc-grid:page:2:element:1",
        document_id="doc-grid",
        source_format="pdf",
        text=(
            "Coastal Microgrid: Galvanic Isolation and Synchronous Rectification\n"
            "Table 4 summarizes quarterly rainfall totals and ambient humidity readings "
            "across northern weather stations."
        ),
        element_type="text",
        page_number=2,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_genuine_caption = Chunk(
        id="doc-grid:page:3:element:2",
        document_id="doc-grid",
        source_format="pdf",
        text=(
            "Figure 2. Coastal Microgrid inverter array relies on galvanic isolation "
            "and synchronous rectification for fault ride-through."
        ),
        element_type="caption",
        page_number=3,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    def _run_case(pool_chunks: list[Chunk]):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        if hasattr(bm25_retriever, "_BM25_CACHE"):
            bm25_retriever._BM25_CACHE.clear()
        init_request_telemetry()

        class _DocRepo:
            def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
                return {"documents": [{"id": "doc-grid", "filename": "microgrid.pdf"}]}

            def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
                return list(pool_chunks)

            def get(self, doc_id: str):
                return None

            def search_vector(self, *args, **kwargs):
                return []

            def check_semantic_cache(self, *args, **kwargs):
                return None

        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace("Header Only WS", "tester", workspace_id=ws)
        b_repo.create_source(ws, "microgrid.pdf", source_id="doc-grid")
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws,
                document_id="doc-grid",
                source="microgrid.pdf",
                version="v1",
                format="pdf",
                created_at="2026-11-26T00:00:00Z",
            )
        )

        doc_repo = _DocRepo()
        qs = QueryService(repo=doc_repo)
        req = QueryRequest(
            query="How does Coastal Microgrid achieve fault ride-through?",
            workspace_id=ws,
            benchmark_mode=True,
            cache=False,
            force_full_path=True,
        )
        ans = (
            "Coastal Microgrid achieves fault ride-through through galvanic isolation "
            "and synchronous rectification."
        )
        raw_json = json.dumps({"answer": ans, "citations": []})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(raw_json, {"input_tokens": 10, "output_tokens": 10})) as mock_gen,
        ):
            out = qs.execute_query(req)
            assert mock_gen.call_count <= 1
            return out

    # 1. Supporting passage present -> cite supporting passage and preserve answer
    res_supported = _run_case([c_supporting, c_header_only])
    assert "galvanic isolation" in res_supported["answer"].lower()
    assert [c["chunk_id"] for c in res_supported["citations"]] == [c_supporting.id]

    # 2. Only heading + unrelated body text -> must refuse
    res_header_only = _run_case([c_header_only])
    assert res_header_only["citations"] == []
    assert res_header_only["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_header_only["answer"].lower()

    # 3. Genuine factual caption stating the fact -> preserve answer and cite caption
    res_caption = _run_case([c_genuine_caption])
    assert "galvanic isolation" in res_caption["answer"].lower()
    assert [c["chunk_id"] for c in res_caption["citations"]] == [c_genuine_caption.id]


def test_training_vs_inference_evidence_in_citation_fallback(monkeypatch):
    """Training-only mechanism mention must not satisfy an inference claim."""
    import json
    from unittest.mock import patch
    from db.workspace_boundary_repo import DocumentSnapshot, InMemoryWorkspaceBoundaryRepository
    from modules.query.query_service import QueryRequest, QueryService
    from schemas.models import Chunk
    from services.retrieval import bm25_retriever
    from services.telemetry import init_request_telemetry

    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("RERANKER_PROVIDER", "lexical")
    ws = "ws_train_vs_infer"

    c_infer = Chunk(
        id="doc-vortex:page:2:element:0",
        document_id="doc-vortex",
        source_format="pdf",
        text=(
            "VortexNet performs block-sparse gating on fixed-size state buffers. "
            "Because buffer dimensions stay constant, inference latency remains independent of sequence length."
        ),
        element_type="text",
        page_number=2,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_train_only = Chunk(
        id="doc-vortex:page:4:element:1",
        document_id="doc-vortex",
        source_format="pdf",
        text=(
            "VortexNet relies on block-sparse gating to achieve O(N) training complexity during pretraining. "
            "Separate benchmarks evaluate inference throughput across batch sizes."
        ),
        element_type="text",
        page_number=4,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )
    c_kv = Chunk(
        id="doc-vortex:page:6:element:2",
        document_id="doc-vortex",
        source_format="pdf",
        text="Baseline models apply KV-window pruning during inference to bound memory growth.",
        element_type="text",
        page_number=6,
        workspace_id=ws,
        version="v1",
        document_version="v1",
    )

    def _run_case(pool_chunks: list[Chunk]):
        if hasattr(bm25_retriever, "_CHUNK_CACHE"):
            bm25_retriever._CHUNK_CACHE.clear()
        if hasattr(bm25_retriever, "_BM25_CACHE"):
            bm25_retriever._BM25_CACHE.clear()
        init_request_telemetry()

        class _DocRepo:
            def get_workspace_documents(self, workspace_id: str, page: int = 1, limit: int = 1000):
                return {"documents": [{"id": "doc-vortex", "filename": "vortex.pdf"}]}

            def get_all_chunks(self, document_ids=None, workspace_id: str = ""):
                return list(pool_chunks)

            def get(self, doc_id: str):
                return None

            def search_vector(self, *args, **kwargs):
                return []

            def check_semantic_cache(self, *args, **kwargs):
                return None

        b_repo = InMemoryWorkspaceBoundaryRepository()
        b_repo.create_workspace("Train vs Infer WS", "tester", workspace_id=ws)
        b_repo.create_source(ws, "vortex.pdf", source_id="doc-vortex")
        b_repo.save_document_snapshot(
            DocumentSnapshot(
                workspace_id=ws,
                document_id="doc-vortex",
                source="vortex.pdf",
                version="v1",
                format="pdf",
                created_at="2026-11-26T00:00:00Z",
            )
        )

        doc_repo = _DocRepo()
        qs = QueryService(repo=doc_repo)
        req = QueryRequest(
            query="How does VortexNet keep inference latency independent of sequence length?",
            workspace_id=ws,
            benchmark_mode=True,
            cache=False,
            force_full_path=True,
        )
        ans = (
            "VortexNet maintains constant inference latency independent of sequence length. "
            "This is achieved through block-sparse gating and KV-window pruning in baseline models."
        )
        raw_json = json.dumps({"answer": ans, "citations": []})
        with (
            patch("src.db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("db.workspace_boundary_repo.get_workspace_boundary_repo", return_value=b_repo),
            patch("services.langgraph_pipeline._get_repo", return_value=doc_repo),
            patch("services.langgraph_pipeline.check_fidelity", return_value=True),
            patch("services.llm.llm_service.generate_completion", return_value=(raw_json, {"input_tokens": 10, "output_tokens": 10})) as mock_gen,
        ):
            out = qs.execute_query(req)
            assert mock_gen.call_count <= 1
            return out

    # 1. With inference passage present -> cite c_infer and c_kv, not c_train_only
    res_with_infer = _run_case([c_infer, c_train_only, c_kv])
    cited_ids = {c["chunk_id"] for c in res_with_infer["citations"]}
    assert cited_ids == {c_infer.id, c_kv.id}
    assert c_train_only.id not in cited_ids

    # 2. With inference passage removed (only training-only + KV-window) -> must refuse
    res_no_infer = _run_case([c_train_only, c_kv])
    assert res_no_infer["citations"] == []
    assert res_no_infer["retained_evidence_items"] == []
    assert "couldn't find any relevant" in res_no_infer["answer"].lower()
