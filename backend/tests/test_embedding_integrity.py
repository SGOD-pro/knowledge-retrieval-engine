from unittest.mock import patch

import pytest

from schemas.models import Chunk, Document
from src.db.database import CloudRepository
from src.ingestion.embed_service import embed_chunks_dual


def _make_sample_chunk(
    cid="c1", text="Sample chunk text", f_emb=None, full_emb=None, page_number=None
):
    return Chunk(
        id=cid,
        document_id="doc1",
        source_format="docx",
        text=text,
        element_type="paragraph",
        page_number=page_number,
        section_path=(),
        bounding_box=None,
        location_reference="p1",
        metadata={},
        structural_weight=1.0,
        provider="dev",
        embedding_fast=f_emb,
        embedding_full=full_emb,
        image_s3_keys=(),
        workspace_id="ws_test",
    )


def test_embed_chunks_dual_fails_loudly_on_api_error():
    """AE2: embed_chunks_dual must raise an exception when API embedding fails,
    instead of silently returning None or zero-padded vectors."""
    chunks = [_make_sample_chunk()]
    with patch(
        "providers.embedding_provider.embed_batch",
        side_effect=RuntimeError("Bedrock Titan API Timeout"),
    ):
        with pytest.raises(RuntimeError, match="Bedrock Titan API Timeout"):
            embed_chunks_dual(chunks)


def test_embed_batch_rejects_zero_padding():
    """AE1: embed_batch must never generate zero-padded vectors for embedding_full."""
    from providers.embedding_provider import embed_batch

    # In dev mode, embed_batch should NOT return 1024-dim vectors where the last 640 dims are all 0.0
    with patch(
        "providers.embedding_provider.embed_text", side_effect=RuntimeError("API down")
    ):
        with pytest.raises(RuntimeError):
            embed_batch(["Some text"], provider="dev")


def test_cloud_repository_save_rejects_null_embeddings():
    """AE2: CloudRepository.save must raise ValueError if any chunk has null embeddings."""
    repo = CloudRepository()
    chunk_bad = _make_sample_chunk(f_emb=None, full_emb=None)
    doc = Document(
        id="doc1",
        filename="test.docx",
        source_format="docx",
        chunks=(chunk_bad,),
        workspace_id="ws_test",
    )

    with pytest.raises((ValueError, RuntimeError), match="[Ii]ntegrity|embedding"):
        repo.save(doc)


def test_cloud_repository_save_rejects_zero_padded_embeddings():
    """AE2: CloudRepository.save must raise ValueError if embedding_full has trailing zeros (zero-pad hack)."""
    repo = CloudRepository()
    # 384 real values, 640 zeros
    fake_full = [0.1] * 384 + [0.0] * 640
    fake_fast = [0.1] * 384
    chunk_bad = _make_sample_chunk(f_emb=fake_fast, full_emb=fake_full)
    doc = Document(
        id="doc1",
        filename="test.docx",
        source_format="docx",
        chunks=(chunk_bad,),
        workspace_id="ws_test",
    )

    with pytest.raises(
        (ValueError, RuntimeError), match="[Ii]ntegrity|embedding|zero-padded"
    ):
        repo.save(doc)


def test_cloud_repository_save_rejects_dimension_mismatch():
    """AE2: CloudRepository.save must raise ValueError if embedding dimensions do not match 384/1024."""
    repo = CloudRepository()
    fake_fast = [0.1] * 1024
    fake_full = [0.1] * 384
    chunk_bad = _make_sample_chunk(f_emb=fake_fast, full_emb=fake_full)
    doc = Document(
        id="doc1",
        filename="test.docx",
        source_format="docx",
        chunks=(chunk_bad,),
        workspace_id="ws_test",
    )

    with pytest.raises(
        (ValueError, RuntimeError), match="[Ii]ntegrity|embedding|dimension"
    ):
        repo.save(doc)
