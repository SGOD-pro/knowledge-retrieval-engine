import os
from dotenv import load_dotenv
# We must load .env from the current script dir (backend)
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.config import settings
from src.services.retrieval.vector_retriever import VectorRetriever
import time

print("Loaded Qdrant URL:", settings.QDRANT_URL)

try:
    vr = VectorRetriever()
    results = vr.search("What is the capital of India?", top_k=2)
    print(f"Direct check successful! Retrieved {len(results)} chunks.")
    for chunk, score in results:
        print(f"Chunk ID: {chunk.id}, Score: {score}")
except Exception as e:
    import traceback
    traceback.print_exc()
