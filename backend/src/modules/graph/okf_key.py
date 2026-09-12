"""Shared OKF entity key canonicalization.

Single source of truth for how raw concept names are converted to DynamoDB
partition-key segments.  Both ingestion (write path) and query (read path) must
import this function so that written keys and queried keys are always identical.

Key format: UPPER_SNAKE_CASE, e.g. "Multi-Head Attention" → "MULTI_HEAD_ATTENTION"

DO NOT duplicate this logic anywhere else.  Any change here automatically
applies to both read and write paths.
"""


def canon_key(name: str) -> str:
    """Return the canonical OKF concept key segment for *name*.

    This is the segment that appears after the ``ENTITY#`` prefix in DynamoDB
    partition keys (okf_entities, okf_properties, okf_relations tables).

    Rules:
    - Strip leading/trailing whitespace
    - Upper-case
    - Replace all whitespace and hyphens with underscores

    Examples::

        >>> canon_key("Multi-Head Attention")
        'MULTI_HEAD_ATTENTION'
        >>> canon_key("  layer normalization  ")
        'LAYER_NORMALIZATION'
        >>> canon_key("BERT")
        'BERT'
    """
    return name.strip().upper().replace("-", "_").replace(" ", "_")
