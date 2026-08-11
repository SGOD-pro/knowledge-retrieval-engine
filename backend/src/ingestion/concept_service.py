"""OKF Concept Service — deterministic gating and scoring before LLM extraction.

System 1: Smart OKF Extraction (Token Optimization)

Step A: Tier-1 Regex Signal Scoring
  For every chunk, calculate tier1_signal_score:
  +1 per DATE_PATTERN match    (Q1-Q4, YYYY years, month names)
  +1 per CURRENCY_PATTERN match ($, %, M/B/K suffixes)
  +1 per CAPITALIZED_ENTITY match (2+ consecutive capitalized words)
  GATE: If score == 0, skip chunk entirely (okf_skipped=True).

Step B: Deduplication
  chunk_hash = sha256(normalize_text(chunk.text))
  Skip if already seen in this document (boilerplate/headers).

Step C: Priority Score for Batching
  extraction_priority_score = (tier1_signal_score * 0.6) + (structural_weight * 0.4)
  Sort descending — highest-value chunks go to Nova Micro first.
"""

import hashlib
import logging
import re

from schemas.models import Chunk

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Compiled patterns (module-level — compiled once at import)
# ---------------------------------------------------------------------------

_DATE_PATTERN = re.compile(
    r"\b(?:Q[1-4]|20\d{2}|19\d{2}|"
    r"January|February|March|April|May|June|July|August|"
    r"September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b",
    re.IGNORECASE,
)

_CURRENCY_PATTERN = re.compile(
    r"(?:\$[\d,]+(?:\.\d+)?(?:[MBKmk]b?)?\b"  # $1.2M, $500K
    r"|\b\d+(?:\.\d+)?%"                       # 12%, 0.5%
    r"|\b\d+(?:\.\d+)?\s*(?:million|billion|thousand)\b"  # 12 million
    r"|\b\d+(?:\.\d+)?[MBK]\b)",               # 5B, 300M
    re.IGNORECASE,
)

_CAPITALIZED_ENTITY = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b"   # 2+ consecutive Title Case words
)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------

def compute_tier1_score(text: str) -> int:
    """Calculate tier1_signal_score for a single piece of text.

    Returns an integer: 0 means no extractable signal (chunk should be skipped).
    """
    score = 0
    score += len(_DATE_PATTERN.findall(text))
    score += len(_CURRENCY_PATTERN.findall(text))
    score += len(_CAPITALIZED_ENTITY.findall(text))
    return score


def _normalize_text(text: str) -> str:
    """Lowercase + collapse whitespace for hash deduplication."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _chunk_hash(text: str) -> str:
    return hashlib.sha256(_normalize_text(text).encode("utf-8")).hexdigest()


def gate_and_rank_chunks(chunks: list[Chunk]) -> list[tuple[Chunk, int]]:
    """Apply Tier-1 gate + deduplication + priority ranking.

    Returns a list of (chunk, tier1_signal_score) tuples sorted descending
    by extraction_priority_score. Only chunks that pass the gate (score > 0)
    and are not duplicates are returned.

    Args:
        chunks: All chunks from a single document.

    Returns:
        List of (Chunk, tier1_signal_score) tuples, sorted by priority.
    """
    seen_hashes: set[str] = set()
    scored: list[tuple[Chunk, int, float]] = []  # (chunk, tier1, priority)

    for chunk in chunks:
        # Step A: signal gate
        score = compute_tier1_score(chunk.text)
        if score == 0:
            logger.debug("okf.gate_skipped chunk_id=%s (no signal)", chunk.id)
            continue

        # Step B: deduplication
        h = _chunk_hash(chunk.text)
        if h in seen_hashes:
            logger.debug("okf.dedup_skipped chunk_id=%s (duplicate)", chunk.id)
            continue
        seen_hashes.add(h)

        # Step C: priority score
        sw = float(chunk.structural_weight) if chunk.structural_weight else 1.0
        priority = (score * 0.6) + (sw * 0.4)
        scored.append((chunk, score, priority))

    # Sort descending by priority
    scored.sort(key=lambda x: x[2], reverse=True)

    result = [(c, s) for c, s, _ in scored]
    logger.info(
        "okf.gate total=%d passed=%d skipped=%d",
        len(chunks), len(result), len(chunks) - len(result),
    )
    return result


def extract_tier1_patterns(chunks: list[Chunk]) -> list[dict]:
    """Tier-1 regex extraction — zero LLM calls.

    Extracts DATE, CURRENCY, and CAPITALIZED_ENTITY facts from chunks that
    pass the gate. Kept for backwards compatibility with okf_builder.py.
    """
    results = []

    for chunk in chunks:
        # Dates
        for match in _DATE_PATTERN.finditer(chunk.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Date",
                "property_value": match.group(0),
                "source_chunk_id": chunk.id,
                "confidence": 1.0,
            })
        # Currency / percentages
        for match in _CURRENCY_PATTERN.finditer(chunk.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "CurrencyOrPercent",
                "property_value": match.group(0),
                "source_chunk_id": chunk.id,
                "confidence": 1.0,
            })
        # Named entities
        for match in _CAPITALIZED_ENTITY.finditer(chunk.text):
            entity = match.group(1)
            # Skip very generic short patterns
            if len(entity) < 5:
                continue
            results.append({
                "concept": entity,
                "property_name": "Identifier",
                "property_value": entity,
                "source_chunk_id": chunk.id,
                "confidence": 0.85,
            })

    return results
