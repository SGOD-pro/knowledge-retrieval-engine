import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from src.config import settings

# Force threshold down to see results
settings.VECTOR_THRESHOLD = -1.0

from src.services.retrieval.vector_retriever import VectorRetriever

vr = VectorRetriever()
results = vr.search("NITI Aayog National Strategy", top_k=2, fast_path=False)
print(f"Direct check successful! Retrieved {len(results)} chunks.")
for chunk, score in results:
    print(f"Chunk ID: {chunk.id}, Score: {score}")
