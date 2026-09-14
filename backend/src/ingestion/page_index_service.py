import re

from schemas.models import Chunk

_WEIGHTS = {
    "title": 3.0,
    "heading": 2.5,
    "section": 2.0,
    "paragraph": 1.0,
    "cell": 1.0,
    "caption": 0.8,
    "footnote": 0.75,
}


def score(chunk: Chunk, query: str) -> float:
    terms = set(re.findall(r"[\w]+", query.lower()))
    chunk_terms = set(re.findall(r"[\w]+", chunk.text.lower()))
    matches = len(terms & chunk_terms)
    depth_decay = 1 / (1 + max(0, len(chunk.section_path) - 1) * 0.1)
    base = matches * _WEIGHTS.get(chunk.element_type, 1.0) * depth_decay

    # Specific structural entity boost (e.g. Table 8, Figure 5, Note 3, Appendix II)
    target_entities = re.findall(
        r"\b(table\s+\d+|figure\s+\d+|note\s+\d+|appendix\s+[a-z0-9]+)\b",
        query.lower(),
    )
    for te in target_entities:
        if te in chunk.text.lower():
            base *= 3.0
            break

    return base


def rank(chunks: list[Chunk], query: str, limit: int = 5) -> list[Chunk]:
    return sorted(chunks, key=lambda chunk: score(chunk, query), reverse=True)[:limit]
