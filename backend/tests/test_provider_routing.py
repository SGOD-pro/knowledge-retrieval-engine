import os
import pytest
from unittest.mock import patch

from kre.shared.providers.provider_client import get_active_provider, ConfigurationError
from kre.shared.providers.embedding_provider import _PROD_MODEL_ID as EMB_PROD, _DEV_MODEL_ID as EMB_DEV
from kre.shared.providers.reranker_provider import _PROD_MODEL_ID as RERANK_PROD, _DEV_MODEL_ID as RERANK_DEV
from kre.shared.providers.llm_provider import _PROD_MODEL_ID as LLM_PROD, _DEV_MODEL_ID as LLM_DEV

def test_r28_provider_routing_enforced():
    """Rule 28: All models must map exactly to the Model Provider Matrix."""
    assert EMB_PROD == "amazon.titan-embed-text-v2:0"
    assert EMB_DEV == "nvidia/nemotron-3-embed-1b"
    
    assert RERANK_PROD == "cohere.rerank-v3-5:0"
    assert RERANK_DEV == "nvidia/llama-nemotron-rerank-vl-1b-v2"
    
    assert LLM_PROD == "amazon.nova-lite-v1:0"
    assert LLM_DEV == "nvidia/nemotron-nano-9b-v2:free"

@patch.dict(os.environ, {"ENVIRONMENT": "production", "MODEL_PROVIDER": "dev"}, clear=True)
def test_r29_no_dev_in_prod():
    """Rule 29: MODEL_PROVIDER=dev is prohibited in production environment."""
    with pytest.raises(ConfigurationError, match="MODEL_PROVIDER=dev is strictly prohibited in production"):
        get_active_provider()

@patch.dict(os.environ, {"ENVIRONMENT": "development", "MODEL_PROVIDER": "dev"}, clear=True)
def test_dev_in_dev_allowed():
    assert get_active_provider() == "dev"

@patch.dict(os.environ, {"ENVIRONMENT": "production", "MODEL_PROVIDER": "prod"}, clear=True)
def test_prod_in_prod_allowed():
    assert get_active_provider() == "prod"
