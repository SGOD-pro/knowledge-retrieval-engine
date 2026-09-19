"""Base protocol and failure classifications for Evidence Providers."""

from typing import Any, Protocol

from schemas.evidence import EvidenceEnvelope


class OptionalProviderFailure(Exception):
    """Failure in an optional enrichment provider (e.g. Graph, OKF); execution continues."""
    pass


class DegradedProviderFailure(Exception):
    """Failure in a primary provider with available fallback (e.g. Vector timeout falling back to BM25)."""
    pass


class CriticalStorageFailure(Exception):
    """Critical failure in core storage/auth (e.g. workspace auth failure, DB corruption). Must fail explicitly."""
    pass


class EvidenceProvider(Protocol):
    """Standardized retrieval provider protocol."""

    provider_name: str

    def retrieve(
        self,
        query: str,
        workspace_id: str,
        top_k: int = 20,
        **kwargs: Any,
    ) -> list[EvidenceEnvelope]:
        """Retrieve standardized evidence envelopes.

        Workspace isolation is mandatory and enforced on every call.
        """
        ...
