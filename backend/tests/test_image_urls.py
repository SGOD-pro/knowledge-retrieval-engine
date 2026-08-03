"""Tests for build_citation() image URL generation.

Verifies that:
  - Chunks with image_s3_keys produce populated citation.image_urls.
  - Chunks without image_s3_keys produce empty citation.image_urls.
  - The presigned URL call uses the correct bucket and key.
  - A boto3 presign failure is swallowed — the query response is not broken.
  - get_boto3_client("s3") is used (no raw boto3.client calls).

No real AWS calls are made — get_boto3_client is mocked end-to-end.
"""

import uuid
from unittest.mock import MagicMock, patch

import pytest

from kre.shared.models import Chunk
from kre.query_lambda.retrieval.response_builder import build_citation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_chunk(
    image_s3_keys: tuple[str, ...] = (),
    source_format: str = "pdf",
    page_number: int = 1,
) -> Chunk:
    doc_id = str(uuid.uuid4())
    return Chunk(
        id=f"{doc_id}:page:{page_number}:element:0",
        document_id=doc_id,
        source_format=source_format,
        text="Sample chunk text for testing purposes.",
        element_type="paragraph",
        page_number=page_number,
        bounding_box={"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 1.0},
        location_reference=f"Page: {page_number}",
        image_s3_keys=image_s3_keys,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildCitationImageUrls:
    """build_citation() with image_s3_keys produces image_urls via presigned S3."""

    def test_chunk_with_no_image_keys_has_empty_image_urls(self):
        """Chunks without images produce an empty image_urls tuple."""
        chunk = _make_chunk(image_s3_keys=())

        # get_boto3_client must NOT be called if there are no keys.
        with patch(
            "kre.query_lambda.retrieval.response_builder.get_signed_image_url"
        ) as mock_sign:
            citation = build_citation(chunk, image_bucket="test-bucket")

        mock_sign.assert_not_called()
        assert citation.image_urls == ()
        assert citation.to_dict()["image_urls"] == []

    def test_chunk_with_one_image_key_produces_one_url(self):
        """Single image_s3_key → single presigned URL in citation."""
        chunk = _make_chunk(image_s3_keys=("extractions/doc-abc/page-1.png",))
        expected_url = "https://s3.example.com/presigned/page-1.png?X-Amz-Signature=abc"

        with patch(
            "kre.query_lambda.retrieval.response_builder.get_signed_image_url",
            return_value=expected_url,
        ) as mock_sign:
            citation = build_citation(chunk, image_bucket="my-bucket")

        mock_sign.assert_called_once_with("extractions/doc-abc/page-1.png", "my-bucket")
        assert citation.image_urls == (expected_url,)

    def test_chunk_with_multiple_image_keys_produces_multiple_urls(self):
        """Multiple image_s3_keys produce one URL per key, in order."""
        keys = (
            "extractions/doc-xyz/page-1.png",
            "extractions/doc-xyz/page-2.png",
            "extractions/doc-xyz/figure-3.jpg",
        )
        chunk = _make_chunk(image_s3_keys=keys)

        def fake_sign(key: str, bucket: str, **kwargs) -> str:
            return f"https://presigned/{key}"

        with patch(
            "kre.query_lambda.retrieval.response_builder.get_signed_image_url",
            side_effect=fake_sign,
        ):
            citation = build_citation(chunk, image_bucket="test-bucket")

        assert len(citation.image_urls) == 3
        assert citation.image_urls[0] == "https://presigned/extractions/doc-xyz/page-1.png"
        assert citation.image_urls[2] == "https://presigned/extractions/doc-xyz/figure-3.jpg"

    def test_presign_failure_is_swallowed_not_propagated(self):
        """A boto3 ClientError during presign must not raise — the key is skipped."""
        from botocore.exceptions import ClientError

        keys = ("extractions/bad-key.png", "extractions/good-key.png")
        chunk = _make_chunk(image_s3_keys=keys)

        call_count = 0

        def side_effect(key: str, bucket: str, **kwargs) -> str:
            nonlocal call_count
            call_count += 1
            if "bad-key" in key:
                raise ClientError(
                    {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                    "generate_presigned_url",
                )
            return f"https://presigned/{key}"

        with patch(
            "kre.query_lambda.retrieval.response_builder.get_signed_image_url",
            side_effect=side_effect,
        ):
            citation = build_citation(chunk, image_bucket="test-bucket")

        # bad-key skipped, good-key present
        assert call_count == 2
        assert len(citation.image_urls) == 1
        assert "good-key" in citation.image_urls[0]

    def test_bucket_falls_back_to_env_default_when_not_passed(self, monkeypatch):
        """When image_bucket is None, S3_BUCKET_NAME env var is used."""
        monkeypatch.setenv("S3_BUCKET_NAME", "env-fallback-bucket")

        # Reimport the module so the module-level _DEFAULT_IMAGE_BUCKET is re-evaluated.
        import importlib
        import kre.query_lambda.retrieval.response_builder as rb
        importlib.reload(rb)

        chunk = _make_chunk(image_s3_keys=("extractions/img.png",))

        captured_buckets: list[str] = []

        def fake_sign(key: str, bucket: str, **kwargs) -> str:
            captured_buckets.append(bucket)
            return f"https://presigned/{key}"

        with patch.object(rb, "get_signed_image_url", side_effect=fake_sign):
            citation = rb.build_citation(chunk, image_bucket=None)

        assert captured_buckets == ["env-fallback-bucket"]
        assert len(citation.image_urls) == 1

    def test_get_boto3_client_not_called_directly(self):
        """image_url_provider must use get_boto3_client, not bare boto3.client."""
        import boto3 as real_boto3

        chunk = _make_chunk(image_s3_keys=("extractions/img.png",))

        with patch(
            "kre.shared.providers.image_url_provider.get_boto3_client"
        ) as mock_factory:
            mock_s3 = MagicMock()
            mock_s3.generate_presigned_url.return_value = "https://presigned/img.png"
            mock_factory.return_value = mock_s3

            with patch("boto3.client") as mock_raw:
                citation = build_citation(chunk, image_bucket="test-bucket")
                # raw boto3.client() must never be called by the provider
                mock_raw.assert_not_called()

            mock_factory.assert_called_with("s3")
            mock_s3.generate_presigned_url.assert_called_once_with(
                "get_object",
                Params={"Bucket": "test-bucket", "Key": "extractions/img.png"},
                ExpiresIn=3600,
            )

    def test_to_dict_serialises_image_urls_as_list(self):
        """Citation.to_dict() must serialize image_urls as a plain list for JSON."""
        chunk = _make_chunk(image_s3_keys=("extractions/img.png",))

        with patch(
            "kre.query_lambda.retrieval.response_builder.get_signed_image_url",
            return_value="https://presigned/img.png",
        ):
            citation = build_citation(chunk, image_bucket="test-bucket")

        d = citation.to_dict()
        assert isinstance(d["image_urls"], list)
        assert d["image_urls"] == ["https://presigned/img.png"]
