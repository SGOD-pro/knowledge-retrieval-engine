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
