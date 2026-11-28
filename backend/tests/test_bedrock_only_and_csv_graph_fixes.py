"""Focused regressions for Bedrock-only runtime, dev/prod resource isolation, and CSV Graph schema identity."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from config import Settings
from db.knowledge_graph_repo import InMemoryKnowledgeGraphRepository
from db.table_store.memory_store import MemoryTableStore
from db.table_store.sqlite_store import SQLiteTableStore
from ingestion.graph_builder import (
    DirectionalSchemaRelation,
    KnowledgeGraphBuilder,
    _resolve_schema_relations_from_table_store,
    extract_and_persist_graph,
)
from providers.bedrock_models import get_extraction_model, get_reranker_model
from providers.reranker_provider import resolve_reranker_config
from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    TableCell,
    TableRow,
    TableSchema,
)
from scripts.run_canonical_60_benchmark import get_reranker_policy


def _sample_schema(relations=(), metadata=None) -> TableSchema:
    return TableSchema(
        columns=(
            ColumnDefinition(col_index=0, name="department", inferred_dtype=InferredDtype.STRING),
            ColumnDefinition(col_index=1, name="program", inferred_dtype=InferredDtype.STRING),
        ),
        header_rows=(0,),
        header_topology=HeaderTopology.SINGLE_ROW,
        confidence=1.0,
        schema_relations=tuple(relations),
        metadata=dict(metadata or {}),
    )


def test_openrouter_removed_from_settings_and_reranker():
    s = Settings(ENVIRONMENT="dev", _env_file=None)
    assert not hasattr(s, "OPENROUTER_API_KEY")
    assert not hasattr(s, "OPENROUTER_RERANKER_MODEL")
    assert s.RERANKER_PROVIDER == "bedrock"
    assert s.BEDROCK_RERANKER_ENABLED is True

    cfg = resolve_reranker_config()
    assert cfg["provider"] == "bedrock"
    assert cfg["model"] == "amazon.rerank-v1:0"
    assert get_reranker_model() == "amazon.rerank-v1:0"
    assert get_reranker_policy().startswith("bedrock:amazon.rerank-v1:0:")
    assert resolve_reranker_config("lexical") == {"provider": "lexical", "model": "bm25plus", "region": None}


def test_disabling_remote_reranker_selects_bm25plus_without_remote_call(monkeypatch):
    from config import settings
    from providers.reranker_provider import rerank_documents

    monkeypatch.setattr(settings, "BEDROCK_RERANKER_ENABLED", False)
    monkeypatch.setattr(settings, "RERANKER_PROVIDER", "bedrock")
    monkeypatch.delenv("BEDROCK_RERANKER_ENABLED", raising=False)
    monkeypatch.delenv("RERANKER_PROVIDER", raising=False)

    assert resolve_reranker_config() == {"provider": "lexical", "model": "bm25plus", "region": None}
    with patch("providers.reranker_provider._get_bedrock_rerank_client") as mock_cli:
        scores, tel = rerank_documents("revenue 2028", ["OrionBio 2028 revenue 740", "Unrelated"], return_telemetry=True)
        assert len(scores) == 2
        mock_cli.assert_not_called()
        assert tel["provider"] == "lexical"
        assert tel["model"] == "bm25plus"


def test_normal_dev_aws_routing_ignores_floci_port_4566(monkeypatch):
    from aws.infra import _build_client, _build_resource
    from config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "AWS_REGION", "ap-south-1")
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    created = []

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            self.profile_name = profile_name
            self.region_name = region_name

        def client(self, service, **kwargs):
            created.append(("client", service, self.region_name, kwargs.get("endpoint_url")))
            return MagicMock()

        def resource(self, service, **kwargs):
            created.append(("resource", service, self.region_name, kwargs.get("endpoint_url")))
            return MagicMock()

    with patch("aws.infra.boto3.Session", side_effect=FakeSession):
        _build_client("dynamodb")
        _build_client("s3")
        _build_resource("dynamodb")

    assert created == [
        ("client", "dynamodb", "ap-south-1", None),
        ("client", "s3", "ap-south-1", None),
        ("resource", "dynamodb", "ap-south-1", None),
    ]


def test_dev_and_prod_resource_isolation_and_explicit_overrides():
    dev_s = Settings(ENVIRONMENT="dev", _env_file=None)
    assert dev_s.DYNAMODB_TABLE_NAME == "kre-table-dev"
    assert dev_s.S3_BUCKET_NAME == "kre-documents-dev"
    assert dev_s.S3_BENCHMARK_BUCKET == "kre-benchmarks-dev"
    assert dev_s.S3_PAGEINDEX_BUCKET == "kre-storage-dev"
    assert dev_s.DYNAMODB_OKF_TABLE_NAME == "kre_okf_facts_dev"
    assert dev_s.DYNAMODB_GRAPH_TABLE_NAME == "kre_knowledge_graph_dev"

    prod_s = Settings(ENVIRONMENT="prod", _env_file=None)
    assert prod_s.DYNAMODB_TABLE_NAME == "kre-table"
    assert prod_s.S3_BUCKET_NAME == "kre-documents-prod"
    assert prod_s.S3_BENCHMARK_BUCKET == "kre-benchmarks"
    assert prod_s.S3_PAGEINDEX_BUCKET == "kre-storage"
    assert prod_s.DYNAMODB_OKF_TABLE_NAME == "kre_okf_facts"
    assert prod_s.DYNAMODB_GRAPH_TABLE_NAME == "kre_knowledge_graph"

    override_s = Settings(
        ENVIRONMENT="dev",
        DYNAMODB_TABLE_NAME="custom-table",
        S3_BUCKET_NAME="custom-bucket",
        _env_file=None,
    )
    assert override_s.DYNAMODB_TABLE_NAME == "custom-table"
    assert override_s.S3_BUCKET_NAME == "custom-bucket"
    assert override_s.DYNAMODB_GRAPH_TABLE_NAME == "kre_knowledge_graph_dev"


def test_graph_builder_bedrock_client_and_shared_extraction_model(monkeypatch):
    monkeypatch.setattr("config.settings.ENVIRONMENT", "dev")
    monkeypatch.setattr("config.settings.MODEL_PROVIDER", "prod")
    mock_client = MagicMock()
    with patch("aws.infra.get_client", return_value=mock_client) as mock_get:
        builder = KnowledgeGraphBuilder(repository=InMemoryKnowledgeGraphRepository())
        got = builder._get_bedrock_client()
        assert got is mock_client
        mock_get.assert_called_once()

    assert get_extraction_model() == "apac.amazon.nova-micro-v1:0"


def test_csv_schema_preserves_document_identity_and_relations_and_rejects_mismatches(tmp_path: Path):
    rel = DirectionalSchemaRelation(
        source_column="department",
        target_column="program",
        predicate="FUNDS",
    )
    rows = [
        TableRow(
            row_id="row_1",
            row_index=1,
            cells=(
                TableCell(cell_id="c1", row_index=1, col_index=0, coordinate="A2", raw_value="AYURVEDA", normalized_value="AYURVEDA"),
                TableCell(cell_id="c2", row_index=1, col_index=1, coordinate="B2", raw_value="SANCTION", normalized_value="SANCTION"),
            ),
        )
    ]

    for store in (
        SQLiteTableStore(db_path=str(tmp_path / "tbl.db")),
        MemoryTableStore(),
    ):
        schema = _sample_schema(relations=(rel,), metadata={})
        store.store_schema(
            table_id="mo_post_2024_25",
            workspace_id="ws_1",
            schema=schema,
            document_id="doc_csv_1",
            document_version="v1",
        )
        loaded = store.get_schema("mo_post_2024_25", "ws_1", version="v1")
        assert loaded is not None
        assert loaded.metadata.get("document_id") == "doc_csv_1"
        assert loaded.metadata.get("document_version") == "v1"

        resolved = _resolve_schema_relations_from_table_store(
            store, "mo_post_2024_25", "ws_1", "v1", "doc_csv_1"
        )
        assert len(resolved) == 1
        assert resolved[0].predicate == "FUNDS"

        # Mismatched document_id and mismatched version must be rejected
        assert _resolve_schema_relations_from_table_store(
            store, "mo_post_2024_25", "ws_1", "v1", "doc_other"
        ) == []
        assert _resolve_schema_relations_from_table_store(
            store, "mo_post_2024_25", "ws_1", "v_other", "doc_csv_1"
        ) == []

        store.write_row_batch_with_checkpoint(
            table_id="mo_post_2024_25",
            workspace_id="ws_1",
            document_id="doc_csv_1",
            version="v1",
            rows=rows,
            checkpoint_batch=1,
            source_rows=1,
            attempted_rows=1,
            persisted_rows=1,
            rejected_rows=0,
            parser_version="1.0.0",
            schema_version="1",
            content_hash="h1",
        )
        kg_repo = InMemoryKnowledgeGraphRepository()
        manifest = extract_and_persist_graph(
            workspace_id="ws_1",
            document_id="doc_csv_1",
            document_version="v1",
            format="csv",
            table_name="mo_post_2024_25",
            table_store=store,
            repository=kg_repo,
        )
        assert manifest is not None
        assert manifest.status == "ready"
        assert manifest.relationship_count == 1
