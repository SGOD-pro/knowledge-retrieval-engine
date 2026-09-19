"""Multi-Channel Evidence Fusion Layer using Reciprocal Rank Fusion (RRF).

Critical Invariants:
- Raw provider scores are NEVER directly compared across different providers.
- Fusion uses rank position within each provider list and RRF (k=60).
- Graph relations (RELATIONSHIP) and OKF facts (FACT) are first-class evidence envelopes
  and participate directly in ranking and provenance citations.
- Structural boosting applies multipliers to candidates matching high-priority sections.
"""

from collections import defaultdict
import logging
from typing import Any

from schemas.evidence import EvidenceEnvelope, EvidenceType

logger = logging.getLogger(__name__)


def fuse_evidence_envelopes(
    provider_results: dict[str, list[EvidenceEnvelope]],
    rrf_k: int = 60,
    structural_boost_ids: set[str] | None = None,
    structural_boost_factor: float = 2.5,
    top_k: int = 20,
) -> list[EvidenceEnvelope]:
    """Fuse evidence envelopes across multiple providers using Reciprocal Rank Fusion."""
    if not provider_results:
        return []

    rrf_scores: dict[str, float] = defaultdict(float)
    envelope_map: dict[str, EvidenceEnvelope] = {}
    provider_ranks: dict[str, dict[str, int]] = defaultdict(dict)

    boost_set = structural_boost_ids or set()

    # Calculate RRF score for each unique evidence item per provider list
    for provider_name, envelopes in provider_results.items():
        for rank, env in enumerate(envelopes):
            env_id = env.evidence_id
            envelope_map[env_id] = env
            provider_ranks[env_id][provider_name] = rank

            # Base RRF score from Cormack et al. 2009: 1 / (k + rank + 1)
            score_contribution = 1.0 / (rrf_k + rank + 1)

            # Apply structural boost if candidate is inside target section/page
            if env.document_id in boost_set or env_id in boost_set:
                score_contribution *= structural_boost_factor

            rrf_scores[env_id] += score_contribution

    # Always ensure first-class FACT and RELATIONSHIP evidence get fair visibility
    # by applying a domain factual prior if they passed provider threshold
    for env_id, env in envelope_map.items():
        if env.evidence_type in (EvidenceType.FACT, EvidenceType.RELATIONSHIP):
            rrf_scores[env_id] += 0.005  # Slight factual prior boost

    # Sort envelopes strictly by merged RRF score (never raw scores)
    sorted_ids = sorted(rrf_scores.keys(), key=lambda eid: rrf_scores[eid], reverse=True)

    fused_results: list[EvidenceEnvelope] = [envelope_map[eid] for eid in sorted_ids[:top_k]]
    logger.debug(
        "fuse_evidence_envelopes: fused %d providers into %d final envelopes",
        len(provider_results),
        len(fused_results),
    )
    return fused_results
