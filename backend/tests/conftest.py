import os
import pytest

# Ensure all tests run with ENVIRONMENT=test unless explicitly overridden
os.environ["ENVIRONMENT"] = "test"

from config import settings
settings.ENVIRONMENT = "test"


@pytest.fixture(autouse=True)
def _clean_bm25_chunk_cache():
    from services.retrieval.bm25_retriever import invalidate_chunk_cache
    invalidate_chunk_cache()
    yield
    invalidate_chunk_cache()
