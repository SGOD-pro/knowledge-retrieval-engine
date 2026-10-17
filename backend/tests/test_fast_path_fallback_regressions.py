import pytest
from unittest.mock import patch
from decimal import Decimal

from schemas.models import Chunk, Document
from db.database import CloudRepository
from ingestion.embed_service import embed_fast_local, _deterministic_vector
from services.langgraph_pipeline import end_fast_path, pipeline
from providers.llm_provider import reset_llm_counter, get_llm_counter


def _setup_doc(repo: CloudRepository, workspace_id: str, doc_id: str, text: str, format: str = "pdf"):
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
                similarity_score=0.85,
            ),
        ),
        workspace_id=workspace_id,
    )
    repo.save(doc)
    return doc


def test_q167_no_longer_crashes_with_unbound_local_error():
    """Reproduce and fix Q167: verified factual extraction must not raise UnboundLocalError."""
    chunk = Chunk(
        id="c_q167",
        document_id="doc_q167",
        text="Selective Dependence Classification (SDC) evaluates interpretability.",
        source_format="pdf",
        page_number=1,
        element_type="paragraph",
        section_path=("Analysis",),
        similarity_score=0.91,
        workspace_id="ws_test",
    )

    state = {
        "query": "What does SDC stand for in Attention Networks?",
        "top_chunks": [chunk],
        "candidate_chunks": [chunk],
        "stage_timings": {"vector_ms": 12.5},
        "verified_answer": "Selective Dependence Classification",
        "verified_chunk": chunk,
        "workspace_id": "ws_test",
    }

    # Must execute cleanly without UnboundLocalError
    result = end_fast_path(state)
    assert result["executed_path"] == "fast"
    assert result["final_answer"] == "Selective Dependence Classification"
    assert result["confidence_score"] == 0.91
    assert len(result["citations"]) == 1
    assert result["citations"][0]["chunk_id"] == "c_q167"
    assert "fast_path_ms" in result["stage_timings"]
    assert result["stage_timings"]["vector_ms"] == 12.5


def test_failed_extraction_cannot_return_unrelated_first_csv_row():
    """When fast path extraction finds no supported answer, it must NOT return
    an arbitrary first chunk or first CSV row with fabricated confidence.
    """
    csv_chunk = Chunk(
        id="c_csv_row1",
        document_id="doc_csv",
        text="Year: 2025. Industry_aggregation_NZSIOC: Level 1. Industry_code_NZSIOC: 99999",
        source_format="csv",
        page_number=1,
        element_type="table_row",
        section_path=("Data",),
        similarity_score=0.45,
        workspace_id="ws_test",
    )

    state = {
        "query": "What is the capital of France?",
        "top_chunks": [csv_chunk],
        "candidate_chunks": [csv_chunk],
        "stage_timings": {"vector_ms": 8.0},
        "workspace_id": "ws_test",
    }

    result = end_fast_path(state)
    # Must NOT return the unrelated CSV row as final_answer
    assert result.get("final_answer") != csv_chunk.text
    # Must NOT fabricate confidence or claim fast path success
    assert result.get("executed_path") != "fast"
    assert result.get("executed_path") == "full"


def test_escalation_executes_existing_full_retrieval_path(monkeypatch):
    """When fast path extraction finds no supported answer, the query must
    escalate to the full retrieval path (reranker, compressor, fidelity, LLM).
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_escalation_test"

    csv_text = "Year: 2025. Industry_aggregation_NZSIOC: Level 1. Industry_code_NZSIOC: 99999"
    doc_csv = Document(
        id="doc_csv_unrelated",
        filename="survay.csv",
        source_format="csv",
        chunks=(
            Chunk(
                id="doc_csv_unrelated:c1",
                document_id="doc_csv_unrelated",
                text=csv_text,
                source_format="csv",
                page_number=1,
                element_type="table_row",
                section_path=("Data",),
                embedding_fast=embed_fast_local("Who was the primary mission commander of the Apollo landing?"),
                embedding_full=_deterministic_vector(csv_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.90,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_csv)

    prose_text = "The primary mission commander was Neil Armstrong. He piloted the lunar module."
    doc_prose = Document(
        id="doc_prose_target",
        filename="apollo.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_prose_target:c1",
                document_id="doc_prose_target",
                text=prose_text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("History",),
                embedding_fast=embed_fast_local("Unrelated text for vector search"),
                embedding_full=_deterministic_vector(prose_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.20,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_prose)

    query = "Who was the primary mission commander of the Apollo landing?"
    reset_llm_counter()
    response = pipeline.run(query, workspace_id=ws_id)

    # Must escalate to full path and generate via LLM
    assert response.planned_path == "fast"
    assert response.executed_path == "full"
    assert response.fast_path is False
    assert "Neil Armstrong" in response.answer
    assert "Year: 2025" not in response.answer
    # Stage timings must include both fast path attempt and subsequent full stages
    assert "fast_path_ms" in response.stage_timings
    assert "reranker_ms" in response.stage_timings
    assert "llm_ms" in response.stage_timings

    # Exactly one LLM generation call occurred (not zero, not multiple)
    llm_stats = get_llm_counter()
    assert llm_stats["generation_calls"] == 1


def test_supported_fast_extraction_still_works_without_generation(monkeypatch):
    """Supported fast extraction (e.g. abbreviation expansion) must complete
    on the fast path with zero LLM generation calls and zero token usage.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_fast_supported_test"

    text = "The Retrieval Augmented Generation (RAG) architecture combines retrieval with generative models."
    _setup_doc(repo, ws_id, "doc_rag", text)

    reset_llm_counter()
    query = "What does RAG stand for in neural architectures?"
    response = pipeline.run(query, workspace_id=ws_id)

    assert response.executed_path == "fast"
    assert response.fast_path is True
    assert "Retrieval Augmented Generation" in response.answer
    assert response.usage == {"input_tokens": 0, "output_tokens": 0}
    assert len(response.citations) > 0
    # Zero LLM generation calls
    assert get_llm_counter()["generation_calls"] == 0


def test_unsupported_evidence_produces_refusal_with_empty_citations(monkeypatch):
    """When query escalates to the full path but no relevant evidence exists,
    the pipeline returns NOT_FOUND and query_service returns structured refusal
    with empty citations.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_refusal_test"

    unrelated_text = "The quick brown fox jumps over the lazy dog in the middle of a sunny afternoon."
    _setup_doc(repo, ws_id, "doc_unrelated", unrelated_text)

    from modules.query.query_service import QueryService
    from schemas.models import QueryRequest

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_id,
        query="What is the total revenue for quantum computing in 2045?",
        cache=False,
    )

    res = qs.execute_query(req)

    # Must produce the standard refusal and empty citations
    assert "couldn't find any relevant passages" in res["answer"].lower()
    assert res["citations"] == []
    assert res["confidence"] == 0.0


def test_adversarial_evidence_outside_fast_top_10_recovered_by_full_retrieval(monkeypatch):
    """Adversarial regression for Requirement 9:
    1. Distractor documents occupy the entire fast top 10.
    2. Supporting evidence is rank 11 or lower in fast retrieval.
    3. Fast extraction cannot answer from the distractors.
    4. Escalation performs genuine full retrieval with top_k=40 and full embeddings.
    5. The supporting chunk enters the full candidate set and top reranked chunks.
    6. Final answer and citations refer to the supporting evidence.
    7. A changed executed_path alone cannot satisfy the test.
    """
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_adv_top10_recovery"

    query = "Who navigated the bathyscaphe Trieste to Challenger Deep?"

    # Create 10 distractor documents that rank higher in fast embedding than the supporting doc
    # but share no significant query terms so fast extractive parsing fails cleanly
    for i in range(1, 11):
        distractor_text = (
            f"Municipal harbor regulations record {i} regarding commercial pier parking, "
            f"berthing fees, and warehouse inventory control."
        )
        doc_distractor = Document(
            id=f"doc_distractor_{i}",
            filename=f"harbor_regulations_{i}.pdf",
            source_format="pdf",
            chunks=(
                Chunk(
                    id=f"doc_distractor_{i}:c1",
                    document_id=f"doc_distractor_{i}",
                    text=distractor_text,
                    source_format="pdf",
                    page_number=1,
                    element_type="paragraph",
                    section_path=("Regulations",),
                    embedding_fast=embed_fast_local(f"{query} item_{i}"),
                    embedding_full=_deterministic_vector(f"harbor regulations pier {i}", 1024),
                    workspace_id=ws_id,
                    similarity_score=0.92,
                ),
            ),
            workspace_id=ws_id,
        )
        repo.save(doc_distractor)

    # Create the supporting document with the actual answer
    # Its fast embedding has lower query similarity than all 10 distractors
    # but its full 1024-dim embedding has high similarity matching the query terms
    supporting_text = (
        "Jacques Piccard and Don Walsh navigated the bathyscaphe Trieste to Challenger Deep in 1960."
    )
    doc_supporting = Document(
        id="doc_supporting_trieste",
        filename="trieste_expedition.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_supporting_trieste:c1",
                document_id="doc_supporting_trieste",
                text=supporting_text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("History",),
                embedding_fast=_deterministic_vector("generic marine craft and ocean research vessel", 384),
                embedding_full=_deterministic_vector(supporting_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.30,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_supporting)

    # 1. Confirm that fast vector retrieval with top_k=10 excludes the supporting chunk
    from services.retrieval.vector_retriever import VectorRetriever
    vr = VectorRetriever(repository=repo)
    fast_results = vr.search(
        query=query,
        fast_path=True,
        workspace_id=ws_id,
        top_k=10,
    )
    fast_chunk_ids = [c.id for c, _ in fast_results]
    assert len(fast_chunk_ids) == 10
    assert "doc_supporting_trieste:c1" not in fast_chunk_ids, (
        "Adversarial fixture violation: supporting chunk must be outside the fast top 10"
    )

    # 2. Reset model counter and run pipeline
    reset_llm_counter()
    response = pipeline.run(query, workspace_id=ws_id)

    # 3. Confirm escalation occurred and executed full path
    assert response.planned_path == "fast"
    assert response.executed_path == "full"
    assert response.fast_path is False
    assert "fast_path_ms" in response.stage_timings
    assert "full_vector_ms" in response.stage_timings

    # 4. Confirm supporting chunk entered genuine full retrieval candidate set
    vector_candidates = response.retrieval_candidates.get("vector", [])
    assert "doc_supporting_trieste:c1" in vector_candidates, (
        "Escalation failed to recover supporting evidence into the full vector candidate set"
    )

    # 5. Confirm supporting chunk was reranked into top chunks and cited
    top_chunk_ids = [c.id for c in response.top_chunks]
    assert "doc_supporting_trieste:c1" in top_chunk_ids

    # 6. Confirm final answer and citations refer to supporting evidence
    assert "Jacques Piccard" in response.answer or "Trieste" in response.answer
    assert any(c.get("chunk_id") == "doc_supporting_trieste:c1" for c in response.citations)

    # 7. Assert a changed executed_path alone cannot satisfy the test:
    # If vector candidates were still only the initial fast candidates, the supporting chunk would be absent
    assert set(vector_candidates) != set(fast_chunk_ids)

    # Exactly one generation call occurred
    assert get_llm_counter()["generation_calls"] == 1


def test_escalation_cross_workspace_evidence_exclusion(monkeypatch):
    """Negative test: Escalation in Workspace A must strictly exclude evidence in Workspace B."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_a = "ws_escalation_tenant_a"
    ws_b = "ws_escalation_tenant_b"

    # Workspace B has the answer
    b_text = "The secret verification credential for project phoenix is PHOENIX_KEY_9999."
    _setup_doc(repo, ws_b, "doc_secret_b", b_text)

    # Workspace A has unrelated administrative distractor with zero query overlap
    a_text = "Administrative guidelines regarding conference room reservations and office supplies."
    _setup_doc(repo, ws_a, "doc_unrelated_a", a_text)

    from modules.query.query_service import QueryService
    from schemas.models import QueryRequest

    qs = QueryService()
    req = QueryRequest(
        workspace_id=ws_a,
        query="What is the secret verification credential for project phoenix?",
        cache=False,
    )

    res = qs.execute_query(req)

    # Must NOT leak Workspace B secret credential
    assert "PHOENIX_KEY_9999" not in res["answer"]
    assert res["citations"] == []
    assert "couldn't find any relevant passages" in res["answer"].lower()


def test_escalation_source_version_isolation(monkeypatch):
    """Boundary test: Escalation full retrieval must respect document_ids scope filters."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_version_isolation"

    query = "Who authorized the upgraded test track speed limit?"

    # Distractor that matches fast path but fails fast extraction
    doc_distractor = Document(
        id="doc_distractor_track",
        filename="distractor.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_distractor_track:c1",
                document_id="doc_distractor_track",
                text="Commercial dock access and pier berthing guidelines.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Guidelines",),
                embedding_fast=embed_fast_local(query),
                embedding_full=_deterministic_vector("dock access", 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_distractor)

    # doc_track_v1 (out of scope)
    doc_v1 = Document(
        id="doc_track_v1",
        filename="track_v1.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_track_v1:c1",
                document_id="doc_track_v1",
                text="Standard speed limit on the test track was 45 mph during baseline trials.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Track",),
                embedding_fast=embed_fast_local("baseline trials"),
                embedding_full=_deterministic_vector("test track speed limit 45 mph", 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_v1)

    # doc_track_v2 (in scope)
    doc_v2 = Document(
        id="doc_track_v2",
        filename="track_v2.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_track_v2:c1",
                document_id="doc_track_v2",
                text="Chief engineer Dr. Aris authorized the upgraded test track speed limit of 75 mph.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Track",),
                embedding_fast=embed_fast_local("upgraded regulations"),
                embedding_full=_deterministic_vector(
                    "Chief engineer Dr. Aris authorized upgraded test track speed limit 75 mph", 1024
                ),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_v2)

    # Explicitly scope query to doc_track_v2 only
    response = pipeline.run(query, workspace_id=ws_id, document_ids=["doc_track_v2"])

    assert response.planned_path == "fast"
    assert response.executed_path == "full"

    # Full retrieval candidates must only contain doc_track_v2, never doc_track_v1
    vector_candidates = response.retrieval_candidates.get("vector", [])
    assert all("doc_track_v1" not in cid for cid in vector_candidates)
    assert any("doc_track_v2" in cid for cid in vector_candidates)
    assert "75 mph" in response.answer or "Aris" in response.answer
    assert any(c.get("chunk_id") == "doc_track_v2:c1" for c in response.citations)


def test_escalation_no_duplicate_generation_and_no_retry_loop(monkeypatch):
    """Boundary test: Escalation must not duplicate generation calls or loop repeatedly."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_no_loop_test"

    query = "Who commanded the lunar module touchdown?"

    # Distractor that matches fast path but fails fast extraction
    doc_distractor = Document(
        id="doc_distractor_mission",
        filename="distractor.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_distractor_mission:c1",
                document_id="doc_distractor_mission",
                text="Administrative protocol for communication channels.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Protocol",),
                embedding_fast=embed_fast_local(query),
                embedding_full=_deterministic_vector("administrative protocol", 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_distractor)

    # Supporting document
    supporting_text = "Commander Neil Armstrong navigated and commanded the lunar module touchdown."
    doc_apollo = Document(
        id="doc_apollo_eagle",
        filename="apollo.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_apollo_eagle:c1",
                document_id="doc_apollo_eagle",
                text=supporting_text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Mission",),
                embedding_fast=embed_fast_local("apollo lunar flight"),
                embedding_full=_deterministic_vector(supporting_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_apollo)

    reset_llm_counter()
    response = pipeline.run(query, workspace_id=ws_id)

    assert response.planned_path == "fast"
    assert response.executed_path == "full"

    # Exactly one generation call
    assert get_llm_counter()["generation_calls"] == 1

    # Stage timings must show single pass execution without loops
    timings = response.stage_timings
    assert "route_query_ms" in timings
    assert "vector_ms" in timings
    assert "full_vector_ms" in timings
    assert "reranker_ms" in timings
    assert "llm_ms" in timings


def test_escalation_telemetry_correctness(monkeypatch):
    """Boundary test: Escalating query telemetry must report planned=fast, executed=full, fast_path=False."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    repo = CloudRepository()
    ws_id = "ws_telemetry_test"

    query = "Who discovered the structure of DNA molecules?"

    # Distractor that matches fast path but fails fast extraction
    doc_distractor = Document(
        id="doc_distractor_chem",
        filename="distractor.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_distractor_chem:c1",
                document_id="doc_distractor_chem",
                text="General university guidelines on laboratory glassware maintenance.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Guidelines",),
                embedding_fast=embed_fast_local(query),
                embedding_full=_deterministic_vector("laboratory glassware maintenance", 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_distractor)

    text = "James Watson and Francis Crick discovered the double helix structure of DNA molecules."
    doc_dna = Document(
        id="doc_dna_discovery",
        filename="dna.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_dna_discovery:c1",
                document_id="doc_dna_discovery",
                text=text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Genetics",),
                embedding_fast=embed_fast_local("genetics research"),
                embedding_full=_deterministic_vector(text, 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_dna)

    response = pipeline.run(query, workspace_id=ws_id)

    assert response.planned_path == "fast"
    assert response.executed_path == "full"
    assert response.fast_path is False
    assert response.structured_aggregate is False
    assert isinstance(response.stage_timings, dict)
    assert "fast_path_ms" in response.stage_timings
    assert "full_vector_ms" in response.stage_timings


def test_untrusted_1024_dimensional_vector_replaced_in_escalation(monkeypatch):
    """An untrusted 1024 dimensional vector in state must NOT be reused solely because of dimension.
    escalate_to_full_retrieval must replace it using the configured full embedding path."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    from providers.bedrock_models import get_embedding_model
    from providers.embedding_provider import embed_text
    from services.langgraph_pipeline import escalate_to_full_retrieval

    query = "What does quantum key distribution guarantee?"
    untrusted_vector = [999.0] * 1024
    expected_fresh_vector = embed_text(query)

    # Case 1: Untrusted vector with no provenance
    state_no_prov = {
        "query": query,
        "workspace_id": "ws_untrusted_emb_unit",
        "query_embedding": untrusted_vector,
        "query_embedding_provenance": None,
        "candidate_chunks": [],
        "stage_timings": {},
    }

    res_no_prov = escalate_to_full_retrieval(state_no_prov)
    assert res_no_prov["query_embedding"] != untrusted_vector
    assert res_no_prov["query_embedding"] == expected_fresh_vector
    assert res_no_prov["query_embedding_provenance"] == get_embedding_model()

    # Case 2: Untrusted vector with mismatched or untrusted provenance
    state_bad_prov = {
        "query": query,
        "workspace_id": "ws_untrusted_emb_unit",
        "query_embedding": untrusted_vector,
        "query_embedding_provenance": "untrusted_arbitrary_model",
        "candidate_chunks": [],
        "stage_timings": {},
    }

    res_bad_prov = escalate_to_full_retrieval(state_bad_prov)
    assert res_bad_prov["query_embedding"] != untrusted_vector
    assert res_bad_prov["query_embedding"] == expected_fresh_vector
    assert res_bad_prov["query_embedding_provenance"] == get_embedding_model()


def test_untrusted_1024_dimensional_vector_replaced_in_pipeline_escalation(monkeypatch):
    """End to end pipeline regression: an untrusted 1024 dimensional vector passed to pipeline
    must be replaced through the configured full embedding path upon escalation, allowing
    correct evidence recovery and single generation without loops."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    from providers.bedrock_models import get_embedding_model
    from providers.embedding_provider import embed_text

    repo = CloudRepository()
    ws_id = "ws_untrusted_pipeline_test"
    query = "What does quantum key distribution guarantee for communication?"

    # Distractor document matching fast path but failing fast extraction
    doc_distractor = Document(
        id="doc_qkd_distractor",
        filename="distractor.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_qkd_distractor:c1",
                document_id="doc_qkd_distractor",
                text="Standard administrative procedures for laboratory network administration.",
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Admin",),
                embedding_fast=embed_fast_local(query),
                embedding_full=_deterministic_vector("administrative procedures for laboratory", 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_distractor)

    target_text = "Quantum key distribution guarantees information theoretic security based on quantum mechanics."
    doc_target = Document(
        id="doc_qkd_target",
        filename="qkd_science.pdf",
        source_format="pdf",
        chunks=(
            Chunk(
                id="doc_qkd_target:c1",
                document_id="doc_qkd_target",
                text=target_text,
                source_format="pdf",
                page_number=1,
                element_type="paragraph",
                section_path=("Science",),
                embedding_fast=embed_fast_local("physics principles"),
                embedding_full=_deterministic_vector(target_text, 1024),
                workspace_id=ws_id,
                similarity_score=0.9,
            ),
        ),
        workspace_id=ws_id,
    )
    repo.save(doc_target)

    # Pass an untrusted 1024 dimensional vector without provenance
    untrusted_vector = [999.0] * 1024
    reset_llm_counter()

    response = pipeline.run(
        query=query,
        workspace_id=ws_id,
        query_embedding=untrusted_vector,
    )

    assert response.planned_path == "fast"
    assert response.executed_path == "full"

    # Untrusted vector was replaced through configured full embedding path
    assert response.query_embedding != untrusted_vector
    assert response.query_embedding == embed_text(query)
    assert response.query_embedding_provenance == get_embedding_model()

    # Evidence recovered and answer generated
    assert len(response.citations) >= 1
    assert any(c["chunk_id"] == "doc_qkd_target:c1" for c in response.citations)
    assert "information theoretic security" in response.answer.lower()

    # Preserves single generation limit
    assert get_llm_counter()["generation_calls"] == 1


def test_trusted_1024_dimensional_vector_with_model_provenance_is_reused(monkeypatch):
    """When a 1024 dimensional vector carries verified model provenance matching the full index,
    escalation reuses it rather than making duplicate provider calls."""
    monkeypatch.setenv("ENVIRONMENT", "test")
    from providers.bedrock_models import get_embedding_model
    from providers.embedding_provider import embed_text
    from services.langgraph_pipeline import escalate_to_full_retrieval

    query = "What does quantum key distribution guarantee?"
    verified_vector = embed_text(query)
    expected_model = get_embedding_model()

    state = {
        "query": query,
        "workspace_id": "ws_trusted_emb_unit",
        "query_embedding": verified_vector,
        "query_embedding_provenance": expected_model,
        "candidate_chunks": [],
        "stage_timings": {},
    }

    # Patch embed_text to prove it is NOT called when provenance is trusted
    with patch("providers.embedding_provider.embed_text") as mock_embed:
        result = escalate_to_full_retrieval(state)
        mock_embed.assert_not_called()

    assert result["query_embedding"] is verified_vector
    assert result["query_embedding_provenance"] == expected_model


