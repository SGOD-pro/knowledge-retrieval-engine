from services.langgraph_pipeline import pipeline


def test_citation_provenance_invariant():
    """Invariant test verifying that every citation in response.citations
    strictly originates from a retrieved chunk in response.top_chunks.
    """
    query = "How many parallel attention heads h are employed in the Multi-Head Attention mechanism of the base Transformer model?"

    # Run the pipeline
    response = pipeline.run(query)

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
