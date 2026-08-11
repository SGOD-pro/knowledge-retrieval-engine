import os
import pytest
from unittest.mock import patch

from providers.provider_client import get_active_provider, ConfigurationError
from providers.bedrock_models import get_embedding_model, get_reranker_model, get_llm_model

def test_r28_provider_routing_enforced():
    """Rule 28: All models must map to the defined model matrix."""
    assert "titan-embed" in get_embedding_model()
    assert "rerank" in get_reranker_model()
    assert "nova" in get_llm_model()

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
