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
