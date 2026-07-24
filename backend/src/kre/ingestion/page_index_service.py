import re

from kre.models import Chunk

_WEIGHTS = {"title": 3.0, "heading": 2.5, "section": 2.0, "paragraph": 1.0, "cell": 1.0, "caption": 0.8, "footnote": 0.75}


def score(chunk: Chunk, query: str) -> float:
    terms = set(re.findall(r"[\w]+", query.lower()))
    matches = len(terms & set(re.findall(r"[\w]+", chunk.text.lower())))
    depth_decay = 1 / (1 + max(0, len(chunk.section_path) - 1) * 0.1)
    return matches * _WEIGHTS.get(chunk.element_type, 1.0) * depth_decay


def rank(chunks: list[Chunk], query: str, limit: int = 5) -> list[Chunk]:
    return sorted(chunks, key=lambda chunk: score(chunk, query), reverse=True)[:limit]
