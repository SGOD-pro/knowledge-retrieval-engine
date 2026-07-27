from kre.models import Chunk
from kre.providers.embedding_provider import embed_batch

def embed_titan(chunks: list[Chunk]) -> list[list[float]]:
    """
    Generate embeddings for chunks using Titan V2 (Prod).
    """
    return embed_batch([c.text for c in chunks], provider="prod")

def embed_nemotron(chunks: list[Chunk]) -> list[list[float]]:
    """
    Generate embeddings for chunks using Dev provider (Nemotron fallback or OpenRouter).
    """
    return embed_batch([c.text for c in chunks], provider="dev")
