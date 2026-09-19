import pytest
from db.database import CloudRepository
from api.routes import repository
from services.retrieval.vector_retriever import VectorRetriever
from services.retrieval.graph_retriever import GraphRetriever
from services.retrieval.okf_retriever import OKFRetriever


def test_cloud_repository_is_singleton():
    """CloudRepository() calls must return the same singleton instance to avoid
    re-establishing database connections per instantiation."""
    repo1 = CloudRepository()
    repo2 = CloudRepository()
    assert repo1 is repo2, "CloudRepository() must return the same singleton instance"


def test_api_repository_helper_returns_singleton():
    """api.routes.repository() must return the same singleton instance."""
    repo = CloudRepository()
    helper_repo = repository()
    assert helper_repo is repo, "api.routes.repository() must return the singleton CloudRepository"


def test_retrievers_share_singleton_repository():
    """Retrievers initialized without explicit repository must share the singleton instance."""
    repo = CloudRepository()
    vec = VectorRetriever()
    graph = GraphRetriever()
    okf = OKFRetriever()

    assert vec.repository is repo, "VectorRetriever must share singleton repository"
    assert graph.repository is repo, "GraphRetriever must share singleton repository"
    assert okf.repository is repo, "OKFRetriever must share singleton repository"
