from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from modules.graph.graph_repository import GraphRepository


def test_get_workspace_graph_data_contract():
    """Regression test for OKF Knowledge Graph data contract bug.
    
    Verifies that get_workspace_graph reads the real schema written by
    okf_builder._write_entities and okf_builder._put_edge, rather than
    fabricated/non-existent fields.
    """
    repo = GraphRepository.__new__(GraphRepository)
    repo.okf_entities_table = MagicMock()
    repo.okf_properties_table = MagicMock()
    repo.okf_relations_table = MagicMock()

    # Exact schema produced by okf_builder._write_entities()
    seeded_entities = [
        {
            "PK": "ENTITY#FASTAPI",
            "SK": "META",
            "name": "FastAPI",
            "document_ids": ["doc_1", "doc_2"],
            "property_count": Decimal("3"),
        },
        {
            "PK": "ENTITY#PYTHON",
            "SK": "META",
            "name": "Python",
            "document_ids": ["doc_1"],
            "property_count": Decimal("5"),
        },
    ]

    # Exact schema produced by okf_builder._put_edge()
    seeded_edges = [
        {
            "PK": "CHUNK#chunk_101",
            "SK": "REL#CONTAINS_FACT#ENTITY#FASTAPI",
            "rel_type": "CONTAINS_FACT",
            "to_id": "ENTITY#FASTAPI",
        },
        {
            "PK": "CHUNK#chunk_101",
            "SK": "REL#SEMANTICALLY_RELATED#CHUNK#chunk_202",
            "rel_type": "SEMANTICALLY_RELATED",
            "to_id": "CHUNK#chunk_202",
            "score": Decimal("0.895"),
        },
    ]

    repo.okf_entities_table.scan.return_value = {"Items": seeded_entities}
    repo.okf_relations_table.scan.return_value = {"Items": seeded_edges}

    result = repo.get_workspace_graph("ws_test")

    nodes = result["nodes"]
    edges = result["edges"]

    # --- Node Assertions ---
    assert len(nodes) == 2
    fastapi_node = next(n for n in nodes if n["id"] == "FASTAPI")
    python_node = next(n for n in nodes if n["id"] == "PYTHON")

    # Label should come from "name" ('FastAPI', 'Python'), not fabricated canonical_name
    assert fastapi_node["label"] == "FastAPI"
    assert python_node["label"] == "Python"

    # Properties should reflect real document_ids count & property_count
    assert fastapi_node["properties"]["doc_count"] == 2
    assert fastapi_node["properties"]["frequency"] == 3
    assert python_node["properties"]["doc_count"] == 1
    assert python_node["properties"]["frequency"] == 5

    # --- Edge Assertions ---
    assert len(edges) == 2

    # Edge 1: CONTAINS_FACT
    edge_1 = edges[0]
    assert edge_1["source"] == "chunk_101", f"Expected 'chunk_101', got '{edge_1['source']}'"
    assert edge_1["target"] == "FASTAPI", f"Expected 'FASTAPI', got '{edge_1['target']}'"
    assert edge_1["label"] == "CONTAINS_FACT", f"Expected 'CONTAINS_FACT', got '{edge_1['label']}'"
    assert edge_1["weight"] == 1.0

    # Edge 2: SEMANTICALLY_RELATED with score
    edge_2 = edges[1]
    assert edge_2["source"] == "chunk_101", f"Expected 'chunk_101', got '{edge_2['source']}'"
    assert edge_2["target"] == "chunk_202", f"Expected 'chunk_202', got '{edge_2['target']}'"
    assert edge_2["label"] == "SEMANTICALLY_RELATED", f"Expected 'SEMANTICALLY_RELATED', got '{edge_2['label']}'"
    assert pytest.approx(edge_2["weight"], rel=1e-3) == 0.895


# ---------------------------------------------------------------------------
# Task 1 regression — entity key canonicalization round-trip
# ---------------------------------------------------------------------------


def test_canon_key_write_read_round_trip():
    """The key produced by the write path (okf_builder._concept_key / canon_key)
    must equal the key used by the read path (graph_repository.get_okf_properties).

    Before the fix the writer produced MULTI_HEAD_ATTENTION while the reader
    queried multi_head_attention — an exact-match miss on every real entity.
    """
    from modules.graph.okf_key import canon_key
    # Representative names; both space and hyphen are normalized to underscore
    cases = [
        ("Multi-Head Attention", "MULTI_HEAD_ATTENTION"),
        ("layer normalization", "LAYER_NORMALIZATION"),
        ("BERT", "BERT"),
        ("Scaled Dot-Product Attention", "SCALED_DOT_PRODUCT_ATTENTION"),
        ("GPT-4", "GPT_4"),
    ]
    for raw, expected in cases:
        result = canon_key(raw)
        assert result == expected, (
            f"canon_key({raw!r}) = {result!r}, expected {expected!r}"
        )


def _extract_pk_from_conditions(conditions_obj) -> str:
    """Pull the PK string from a boto3 ConditionBase (Key('PK').eq(...)) object."""
    # boto3 ConditionBase.get_expression() returns:
    #   {'format': '{0} {operator} {1}', 'operator': '=', 'values': (Key, 'ENTITY#...')}
    try:
        expr = conditions_obj.get_expression()
        return str(expr["values"][-1])
    except Exception:
        return str(conditions_obj)


def test_get_okf_properties_uses_upper_snake_pk():
    """get_okf_properties must query ENTITY#{UPPER_SNAKE} not ENTITY#{lower}."""
    from unittest.mock import MagicMock
    import modules.documents.documents_repository as dr_mod

    repo = GraphRepository.__new__(GraphRepository)
    repo.okf_entities_table = MagicMock()
    repo.okf_properties_table = MagicMock()
    repo.okf_relations_table = MagicMock()
    repo.okf_properties_table.query.return_value = {"Items": []}

    original = getattr(dr_mod, "_is_test_env", None)
    dr_mod._is_test_env = lambda: False
    try:
        repo.get_okf_properties(["Multi-Head Attention"])
    finally:
        if original is not None:
            dr_mod._is_test_env = original

    call_args = repo.okf_properties_table.query.call_args
    assert call_args is not None, "okf_properties_table.query was never called"
    ke = call_args.kwargs.get("KeyConditionExpression")
    pk_value = _extract_pk_from_conditions(ke)
    assert pk_value == "ENTITY#MULTI_HEAD_ATTENTION", (
        f"Expected 'ENTITY#MULTI_HEAD_ATTENTION', got {pk_value!r}"
    )


def test_expand_graph_uses_upper_snake_pk():
    """expand_graph must query ENTITY#{UPPER_SNAKE} not ENTITY#{lower}."""
    from unittest.mock import MagicMock
    import modules.documents.documents_repository as dr_mod

    repo = GraphRepository.__new__(GraphRepository)
    repo.okf_entities_table = MagicMock()
    repo.okf_properties_table = MagicMock()
    repo.okf_relations_table = MagicMock()
    repo.okf_relations_table.query.return_value = {"Items": []}

    original = getattr(dr_mod, "_is_test_env", None)
    dr_mod._is_test_env = lambda: False
    try:
        repo.expand_graph(["layer normalization"], max_hops=1)
    finally:
        if original is not None:
            dr_mod._is_test_env = original

    call_args = repo.okf_relations_table.query.call_args
    assert call_args is not None, "okf_relations_table.query was never called"
    ke = call_args.kwargs.get("KeyConditionExpression")
    pk_value = _extract_pk_from_conditions(ke)
    assert pk_value == "ENTITY#LAYER_NORMALIZATION", (
        f"Expected 'ENTITY#LAYER_NORMALIZATION', got {pk_value!r}"
    )

