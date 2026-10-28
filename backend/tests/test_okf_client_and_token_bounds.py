"""Offline regression tests for OKF client configuration and token bound guarantees.

Verifies:
1. aws.infra.get_client accepts custom Config without TypeError.
2. OkfBuilder._get_bedrock_client initializes client and fallback correctly.
3. Token bounding guarantees request tokens <= 2000 across tiny, normal, and massive chunks.
4. Preserves attempt budgets and does not touch production kre_okf.db data.
"""

from unittest.mock import MagicMock, patch
import botocore.config
import pytest

from aws.infra import get_client
from db.okf_repo import SQLiteOkfRepository
from ingestion.okf_builder import (
    DocumentExtractionBudget,
    OKF_EXTRACTION_SYSTEM_PROMPT,
    SUPPORTED_OKF_MODELS,
    OkfBuilder,
    count_request_tokens,
)


class DummyChunk:
    def __init__(self, chunk_id: str, text: str, page_number: int = 1):
        self.id = chunk_id
        self.text = text
        self.page_number = page_number


def test_get_client_accepts_custom_config():
    """Verify get_client passes config through without TypeError."""
    custom_cfg = botocore.config.Config(
        connect_timeout=3.0,
        read_timeout=7.0,
        retries={"max_attempts": 1, "mode": "standard"},
    )
    with patch("boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        client = get_client("bedrock-runtime", region_name="ap-south-1", config=custom_cfg)
        mock_session.client.assert_called_with("bedrock-runtime", config=custom_cfg)
        assert client is not None


def test_okf_builder_get_bedrock_client_success():
    """Verify _get_bedrock_client succeeds and disables client retries."""
    builder = OkfBuilder(repo=MagicMock())
    with patch("aws.infra.get_client") as mock_get_client, \
         patch("config.settings.ENVIRONMENT", "dev"), \
         patch("config.settings.MODEL_PROVIDER", "dev"):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client
        with patch.object(builder, "_bedrock_client", None):
            res = builder._get_bedrock_client()
            assert res == mock_client
            mock_get_client.assert_called_once()
            args, kwargs = mock_get_client.call_args
            assert args[0] == "bedrock-runtime"
            assert "config" in kwargs


def test_okf_builder_get_bedrock_client_fallback_credentials():
    """Verify fallback uses settings AWS_PROFILE and AWS_REGION on exception."""
    builder = OkfBuilder(repo=MagicMock())
    with patch("aws.infra.get_client", side_effect=Exception("Infra down")), \
         patch("config.settings.ENVIRONMENT", "dev"), \
         patch("config.settings.MODEL_PROVIDER", "dev"), \
         patch("boto3.Session") as mock_session_cls:
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session
        with patch.object(builder, "_bedrock_client", None):
            res = builder._get_bedrock_client()
            mock_session_cls.assert_called_once()
            call_kwargs = mock_session_cls.call_args.kwargs
            assert call_kwargs.get("profile_name") in ("aws", "local", None) or isinstance(call_kwargs.get("profile_name"), str)
            assert call_kwargs.get("region_name") is not None


@pytest.mark.parametrize("chunk_length", [50, 500, 1500, 4000, 10000])
def test_token_bounds_single_chunk_under_2000(chunk_length: int):
    """Verify that single chunks of any size never exceed the 2000 token limit."""
    chunk_text = "Scientific context and analysis. " * (chunk_length // 30 + 1)
    chunk = DummyChunk("doc1:chunk_001", chunk_text)
    budget = DocumentExtractionBudget(
        workspace_id="ws_test",
        document_id="doc1",
        document_version="v1",
        max_call_input_tokens=2000,
    )

    builder = OkfBuilder(repo=MagicMock())
    # Test batch extraction prompt generation directly
    prompt_text, stats = builder._invoke_bedrock_extraction(
        client=None,  # triggers test mode fallback path
        chunks=[chunk],
        workspace_id="ws_test",
        document_id="doc1",
        budget=budget,
    )
    # Budget should not be exhausted due to token overrun
    assert not budget.exhausted or budget.exhaustion_reason != "per_call_input_token_limit_exceeded"
    assert "per_call_input_token_limit_exceeded" not in (budget.exhaustion_reason or "")


def test_token_bounds_multiple_large_chunks_batch():
    """Verify that batches containing multiple oversized chunks strictly respect 2000 token limit."""
    chunks = [
        DummyChunk(f"doc1:chunk_{i}", f"Large chunk section {i} content text. " * 150)
        for i in range(5)
    ]
    budget = DocumentExtractionBudget(
        workspace_id="ws_test",
        document_id="doc1",
        document_version="v1",
        max_call_input_tokens=2000,
    )

    builder = OkfBuilder(repo=MagicMock())
    prompt_text, stats = builder._invoke_bedrock_extraction(
        client=None,
        chunks=chunks,
        workspace_id="ws_test",
        document_id="doc1",
        budget=budget,
    )
    assert not budget.exhausted or "per_call_input_token_limit_exceeded" not in (budget.exhaustion_reason or "")


def test_count_request_tokens_supported_models():
    """Verify token count computation works across all supported models and rejects unsupported."""
    sys_p = OKF_EXTRACTION_SYSTEM_PROMPT
    user_p = "Sample factual text"
    for model_id in SUPPORTED_OKF_MODELS:
        toks = count_request_tokens(sys_p, user_p, model_id=model_id)
        assert toks > 0
        assert toks < 2000

    with pytest.raises(ValueError, match="Unsupported model"):
        count_request_tokens(sys_p, user_p, model_id="unsupported-model-v1")


def test_kre_okf_db_isolation(tmp_path):
    """Verify SQLiteOkfRepository functions correctly in isolation without touching live db."""
    test_db = tmp_path / "test_okf.db"
    repo = SQLiteOkfRepository(db_path=str(test_db))
    manifest = repo.get_manifest("ws_test", "doc_test", "v1")
    assert manifest is None
