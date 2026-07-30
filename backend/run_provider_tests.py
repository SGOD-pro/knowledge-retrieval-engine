import os
import sys

# Add backend/src to path
sys.path.append(os.path.join(os.path.dirname(__file__), "src"))

from kre.shared.providers.provider_client import get_active_provider, ConfigurationError
from kre.shared.providers.embedding_provider import _PROD_MODEL_ID as EMB_PROD, _DEV_MODEL_ID as EMB_DEV
from kre.shared.providers.reranker_provider import _PROD_MODEL_ID as RERANK_PROD, _DEV_MODEL_ID as RERANK_DEV
from kre.shared.providers.llm_provider import _PROD_MODEL_ID as LLM_PROD, _DEV_MODEL_ID as LLM_DEV

def test_r28_provider_routing_enforced():
    assert EMB_PROD == "amazon.titan-embed-text-v2:0", f"Expected titan-embed-text-v2, got {EMB_PROD}"
    assert EMB_DEV == "nvidia/nemotron-3-embed-1b", f"Expected nemotron-3-embed-1b, got {EMB_DEV}"
    
    assert RERANK_PROD == "cohere.rerank-v3-5:0", f"Expected rerank-v3-5, got {RERANK_PROD}"
    assert RERANK_DEV == "nvidia/llama-nemotron-rerank-vl-1b-v2", f"Expected llama-nemotron, got {RERANK_DEV}"
    
    assert LLM_PROD == "amazon.nova-lite-v1:0", f"Expected nova-lite-v1, got {LLM_PROD}"
    assert LLM_DEV == "nvidia/nemotron-nano-9b-v2:free", f"Expected nemotron-nano-9b, got {LLM_DEV}"
    print("test_r28_provider_routing_enforced: PASS")

def test_r29_no_dev_in_prod():
    os.environ["ENVIRONMENT"] = "production"
    os.environ["MODEL_PROVIDER"] = "dev"
    try:
        get_active_provider()
        print("test_r29_no_dev_in_prod: FAIL (Did not raise error)")
    except ConfigurationError as e:
        print("test_r29_no_dev_in_prod: PASS")

def test_dev_in_dev_allowed():
    os.environ["ENVIRONMENT"] = "development"
    os.environ["MODEL_PROVIDER"] = "dev"
    assert get_active_provider() == "dev"
    print("test_dev_in_dev_allowed: PASS")

def test_prod_in_prod_allowed():
    os.environ["ENVIRONMENT"] = "production"
    os.environ["MODEL_PROVIDER"] = "prod"
    assert get_active_provider() == "prod"
    print("test_prod_in_prod_allowed: PASS")

if __name__ == "__main__":
    try:
        test_r28_provider_routing_enforced()
        test_r29_no_dev_in_prod()
        test_dev_in_dev_allowed()
        test_prod_in_prod_allowed()
    except AssertionError as e:
        print(f"FAIL: {e}")
