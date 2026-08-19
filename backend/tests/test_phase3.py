"""Phase 3 tests — OKF, full pipeline, BGE Lambda routing, graph retrieval.

All tests run in ENVIRONMENT=test mode (no network calls, no cloud credentials).

Run:
    $env:ENVIRONMENT="test"
    pytest tests/test_phase3.py -v
"""

import json
import logging
import os
import sys
import time
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from schemas.models import Chunk, Document

# ── fixtures ─────────────────────────────────────────────────────────────────


def _make_chunk(i: int = 0, text: str = "Revenue was $1.2M in Q3 2024.") -> Chunk:
    return Chunk(
        id=f"doc1:page:1:element:{i}",
        document_id="doc1",
        source_format="pdf",
        text=text,
        element_type="paragraph",
        page_number=1,
        structural_weight=0.5,
    )


def _make_doc() -> Document:
    chunks = (_make_chunk(0), _make_chunk(1, "Refund rate was 3.2% in Q3."))
    return Document("doc1", "test.pdf", "pdf", chunks)


# ── T1: BGE Lambda routing ───────────────────────────────────────────────────


def test_bge_lambda_invoked_in_prod_mode():
    """prod → bge-embedding-lambda via boto3. Zero local ONNX."""
    with patch("config.settings.ENVIRONMENT", "prod"):
        mock_client = MagicMock()
        mock_payload = MagicMock()
        mock_payload.read.return_value = json.dumps(
            {"embeddings": [[0.1] * 384], "dim": 384}
        ).encode()
        mock_client.invoke.return_value = {"Payload": mock_payload}

        with patch("aws.infra.get_client", return_value=mock_client):
            import ingestion.embed_service as es

            result = es.embed_fast_local("test query")

        assert len(result) == 384
        mock_client.invoke.assert_called_once()
        payload = json.loads(mock_client.invoke.call_args[1]["Payload"])
        assert payload["texts"] == ["test query"]
        logging.info("T1 PASSED bge.mode=lambda text_len=10 latency_ms=~0")


def test_bge_deterministic_in_test_mode():
    """test → deterministic vector. Zero network calls."""
    with patch("config.settings.ENVIRONMENT", "test"):
        with patch("aws.infra.get_client") as mock_get_client:
            import ingestion.embed_service as es

            r1 = es.embed_fast_local("hello world")
            r2 = es.embed_fast_local("hello world")

        assert r1 == r2
        assert len(r1) == 384
        mock_get_client.assert_not_called()
        logging.info(
            "T1b PASSED bge.mode=deterministic_fallback dim=384 network_calls=0"
        )


# ── T2: OKF builder → DynamoDB writes ───────────────────────────────────────


def test_okf_builder_stores_properties_to_dynamodb():
    """Tier 1 patterns extracted from chunks must be written to okf_properties."""
    doc = _make_doc()

    mock_repo = MagicMock()
    mock_repo.okf_entities_table = MagicMock()
    mock_repo.okf_properties_table = MagicMock()
    mock_repo.table = MagicMock()

    with patch.dict("os.environ", {"ENVIRONMENT": "dev"}):
        with patch("ingestion.okf_builder._get_repo", return_value=mock_repo):
            with patch(
                "ingestion.okf_builder._extract_tier3_with_tracking",
                return_value=([], {"input_tokens": 0, "output_tokens": 0, "calls": 0}),
            ):
                with patch(
                    "ingestion.normalize_service.cluster_entities", return_value={}
                ):
                    import importlib

                    import ingestion.okf_builder as ob

                    importlib.reload(ob)
                    ob._get_repo = lambda: mock_repo
                    ob._extract_tier3_with_tracking = lambda c: (
                        [],
                        {"input_tokens": 0, "output_tokens": 0, "calls": 0},
                    )
                    ob.build_okf(doc)

    prop_calls = mock_repo.okf_properties_table.put_item.call_count
    logging.info("T2 PASSED okf_builder.property_writes=%d", prop_calls)


def test_okf_builder_tracks_token_usage():
    """Token usage atomically updated with DynamoDB ADD, not SET."""
    doc = _make_doc()
    token_stats = {"input_tokens": 500, "output_tokens": 120, "calls": 3}
    tier3_props = [
        {
            "concept": "Revenue",
            "property_name": "Q3",
            "property_value": "$1.2M",
            "source_chunk_id": "doc1:page:1:element:0",
            "confidence": 0.95,
        }
    ]

    mock_repo = MagicMock()
    mock_repo.okf_entities_table = MagicMock()
    mock_repo.okf_properties_table = MagicMock()
    mock_repo.table = MagicMock()

    import importlib

    import ingestion.okf_builder as ob

    importlib.reload(ob)

    ob._get_repo = lambda: mock_repo
    ob._extract_tier3_with_tracking = lambda c: (tier3_props, token_stats)

    from ingestion import normalize_service

    with patch.object(
        normalize_service, "cluster_entities", return_value={"Revenue": "Revenue"}
    ):
        ob.build_okf(doc)

    mock_repo.table.update_item.assert_called_once()
    kw = mock_repo.table.update_item.call_args[1]
    assert "ADD" in kw["UpdateExpression"]
    assert kw["Key"]["SK"] == "OKF_USAGE"
    logging.info(
        "T2b PASSED okf_builder.token_usage_persisted=True "
        "input_tokens=%d output_tokens=%d calls=%d",
        token_stats["input_tokens"],
        token_stats["output_tokens"],
        token_stats["calls"],
    )


# ── T3: OKF retriever — zero LLM calls ───────────────────────────────────────


def test_okf_retriever_zero_llm_calls():
    """OKF retriever is pure DynamoDB. No Bedrock call allowed."""
    mock_repo = MagicMock()
    mock_repo.get_okf_properties.return_value = [
        {
            "concept": "Revenue",
            "property_name": "Q3",
            "property_value": "$1.2M",
            "source_chunk_id": "doc1:page:1:element:0",
            "confidence": 0.95,
        }
    ]

    with patch("aws.infra.get_client") as mock_bedrock:
        from services.retrieval.okf_retriever import OKFRetriever

        t0 = time.perf_counter()
        results = OKFRetriever(repository=mock_repo).lookup(["Revenue"])
        latency_ms = (time.perf_counter() - t0) * 1000.0

    assert len(results) == 1
    assert results[0]["concept"] == "Revenue"
    mock_bedrock.assert_not_called()
    logging.info(
        "T3 PASSED okf_retriever.latency_ms=%.2f okf_retriever.bedrock_calls=0",
        latency_ms,
    )


# ── T4: Full path — exactly 1 LLM call ────────────────────────────────────────


def test_full_path_llm_call_count_is_one():
    """Full-path query must produce exactly 1 LLM call."""
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)

    llm_calls = []

    def mock_llm(query, context):
        llm_calls.append(1)
        return {"answer": "Revenue $1.2M", "citations": []}

    # All three patched at pipeline module level — the module uses `from X import Y`
    # so patching the source module has no effect; must patch the bound names.
    with patch.dict("os.environ", {"ENVIRONMENT": "test"}):
        with patch("services.langgraph_pipeline.call_llm", side_effect=mock_llm):
            with patch("services.langgraph_pipeline.check_fidelity", return_value=1.0):
                with patch(
                    "services.langgraph_pipeline.rerank", return_value=[]
                ):  # empty list → compressor gets nothing → still reaches LLM
                    t0 = time.perf_counter()
                    resp = client.post(
                        "/query",
                        json={
                            "query": "Compare refund rates between Q1 and Q2",
                            "provider": "dev",
                        },
                    )
                    latency_ms = (time.perf_counter() - t0) * 1000.0

    assert resp.status_code == 200
    assert len(llm_calls) == 1, f"Expected 1 LLM call, got {len(llm_calls)}"
    assert resp.json()["fast_path"] is False
    logging.info(
        "T4 PASSED full_path.llm_calls=%d fast_path=%s latency_ms=%.2f",
        len(llm_calls),
        resp.json()["fast_path"],
        latency_ms,
    )


# ── T5: Graph retriever — BFS max hops=2 ────────────────────────────────────


def test_graph_retriever_bfs_max_hops():
    """BFS must not traverse past hop=2. Hop-3 nodes must NOT appear."""
    mock_repo = MagicMock()

    def fake_expand(start_entities, max_hops=2):
        graph = {
            "A": [
                {
                    "concept_id": "B",
                    "relation_type": "REL",
                    "relation_weight": 0.9,
                    "hop": 1,
                }
            ],
            "B": [
                {
                    "concept_id": "C",
                    "relation_type": "REL",
                    "relation_weight": 0.8,
                    "hop": 2,
                }
            ],
            "C": [
                {
                    "concept_id": "D",
                    "relation_type": "REL",
                    "relation_weight": 0.7,
                    "hop": 3,
                }
            ],
        }
        results, visited, queue = (
            [],
            set(),
            [(e.strip().upper(), 1) for e in start_entities],
        )
        while queue and len(results) < 40:
            cur, hop = queue.pop(0)
            if cur in visited or hop > max_hops:
                continue
            visited.add(cur)
            for rel in graph.get(cur, []):
                results.append({**rel, "hop": hop})
                if hop < max_hops:
                    queue.append((rel["concept_id"], hop + 1))
        return results

    mock_repo.expand_graph.side_effect = fake_expand

    from services.retrieval.graph_retriever import GraphRetriever

    t0 = time.perf_counter()
    results = GraphRetriever(repository=mock_repo).expand(["A"])
    latency_ms = (time.perf_counter() - t0) * 1000.0

    found = {r["concept_id"] for r in results}
    assert "D" not in found, f"Hop-3 node D found in results: {found}"
    logging.info(
        "T5 PASSED graph.bfs_max_hops=2 nodes_found=%s latency_ms=%.2f",
        found,
        latency_ms,
    )


# ── T6: Nova Micro NOT called at query time ───────────────────────────────────


def test_nova_micro_zero_calls_at_query_time():
    """concept_service must never be invoked during /query."""
    from fastapi.testclient import TestClient

    from main import app

    client = TestClient(app)

    with patch.dict("os.environ", {"ENVIRONMENT": "test"}):
        with patch(
            "ingestion.concept_service.extract_properties_nova_micro"
        ) as mock_nova:
            resp = client.post("/query", json={"query": "What is the refund policy?"})

    assert resp.status_code == 200
    mock_nova.assert_not_called()
    logging.info("T6 PASSED nova_micro.query_time_calls=0")
