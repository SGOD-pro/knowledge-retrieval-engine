from unittest.mock import MagicMock

import pytest

from schemas.evidence import (
    EvidenceConfidence,
    EvidenceEnvelope,
    EvidenceType,
    FactEvidence,
    RelationshipEvidence,
    TextEvidence,
)
from schemas.manifest import DocumentManifest, SheetManifest, SheetVisibility
from services.retrieval.evidence_fusion import fuse_evidence_envelopes
from services.retrieval.evidence_providers.base import (
    CriticalStorageFailure,
    DegradedProviderFailure,
    OptionalProviderFailure,
)
from services.retrieval.evidence_providers.graph_provider import GraphEvidenceProvider
from services.retrieval.evidence_providers.metadata_provider import MetadataEvidenceProvider
from services.retrieval.evidence_providers.okf_provider import OKFEvidenceProvider


def test_graph_evidence_provider_first_class():
    mock_retriever = MagicMock()
    mock_retriever.expand.return_value = [
        {"source": "Acme", "target": "Beta Corp", "relation": "subsidiary_of", "weight": 0.9}
    ]

    provider = GraphEvidenceProvider(retriever=mock_retriever)
    envelopes = provider.retrieve(query="What is the relation between Acme and Beta?", workspace_id="ws_1")

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.evidence_type == EvidenceType.RELATIONSHIP
    assert isinstance(env.payload, RelationshipEvidence)
    assert env.payload.source_entity == "Acme"
    assert env.payload.target_entity == "Beta Corp"
    assert env.citation_text == "Acme -> subsidiary_of -> Beta Corp"


def test_okf_evidence_provider_first_class():
    mock_retriever = MagicMock()
    mock_retriever.lookup.return_value = [
        {"entity": "Acme", "attribute": "Headquarters", "value": "Tokyo", "confidence": 0.98}
    ]

    provider = OKFEvidenceProvider(retriever=mock_retriever)
    envelopes = provider.retrieve(query="Where is Acme headquarters?", workspace_id="ws_1")

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.evidence_type == EvidenceType.FACT
    assert isinstance(env.payload, FactEvidence)
    assert env.payload.entity == "Acme"
    assert env.payload.value == "Tokyo"


def test_metadata_evidence_provider():
    sheet_hidden = SheetManifest(
        sheet_name="InternalSalaries",
        sheet_index=2,
        visibility=SheetVisibility.HIDDEN,
    )
    manifest = DocumentManifest(
        document_id="doc_fin",
        filename="Q4_Fin.xlsx",
        source_format="xlsx",
        source_size_bytes=5000,
        parser_version="1.0.0",
        detected_sheets=(sheet_hidden,),
    )

    provider = MetadataEvidenceProvider()
    envelopes = provider.retrieve(
        query="Are there any hidden sheets in Q4_Fin?",
        workspace_id="ws_1",
        manifests=[manifest],
    )

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env.evidence_type == EvidenceType.METADATA
    assert "InternalSalaries" in env.citation_text
    assert "hidden" in env.citation_text


def test_critical_failure_on_missing_workspace():
    provider = MetadataEvidenceProvider()
    with pytest.raises(CriticalStorageFailure) as exc:
        provider.retrieve(query="test", workspace_id="")
    assert "workspace_id must be provided" in str(exc.value)


def test_evidence_fusion_rrf_and_structural_boost():
    env_bm25_1 = EvidenceEnvelope(
        evidence_id="c1",
        evidence_type=EvidenceType.TEXT,
        document_id="docA",
        location={},
        provider="bm25",
        provider_score=15.2,  # Raw BM25 score
        payload=TextEvidence(text="Chunk 1 text", element_type="paragraph"),
    )
    env_bm25_2 = EvidenceEnvelope(
        evidence_id="c2",
        evidence_type=EvidenceType.TEXT,
        document_id="docB",
        location={},
        provider="bm25",
        provider_score=10.1,
        payload=TextEvidence(text="Chunk 2 text", element_type="paragraph"),
    )

    env_dense_1 = EvidenceEnvelope(
        evidence_id="c2",  # Present in both providers!
        evidence_type=EvidenceType.TEXT,
        document_id="docB",
        location={},
        provider="dense",
        provider_score=0.91,  # Raw cosine score
        payload=TextEvidence(text="Chunk 2 text", element_type="paragraph"),
    )
    env_dense_2 = EvidenceEnvelope(
        evidence_id="c3",
        evidence_type=EvidenceType.TEXT,
        document_id="docC",
        location={},
        provider="dense",
        provider_score=0.82,
        payload=TextEvidence(text="Chunk 3 text", element_type="paragraph"),
    )

    env_fact = EvidenceEnvelope(
        evidence_id="f1",
        evidence_type=EvidenceType.FACT,
        document_id="docB",
        location={},
        provider="okf",
        provider_score=0.99,
        payload=FactEvidence(entity="Acme", attribute="CEO", value="Alice"),
    )

    provider_results = {
        "bm25": [env_bm25_1, env_bm25_2],
        "dense": [env_dense_1, env_dense_2],
        "okf": [env_fact],
    }

    # Fuse without structural boost
    fused = fuse_evidence_envelopes(provider_results, top_k=5)
    assert len(fused) >= 3

    # c2 was present in both bm25 and dense, so its RRF rank should be top or near top
    fused_ids = [e.evidence_id for e in fused]
    assert "c2" in fused_ids
    assert "f1" in fused_ids  # Fact participates directly!

    # Fuse WITH structural boost on docA (which boosts c1)
    fused_boosted = fuse_evidence_envelopes(
        provider_results,
        structural_boost_ids={"docA"},
        top_k=5,
    )
    assert fused_boosted[0].evidence_id == "c1"  # docA chunk boosted to top!
