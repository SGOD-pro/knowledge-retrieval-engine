import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from config import settings
from services.langgraph_pipeline import pipeline

# Ensure defaults are correct
settings.BM25_THRESHOLD = 0.1
settings.PAGEINDEX_THRESHOLD = 0.1
settings.VECTOR_THRESHOLD = 0.3
settings.RERANKER_THRESHOLD = 0.5

print("\n=== Run 1: Default Settings ===")
pipeline.run("What is the capital of India?", document_ids=None)

print("\n=== Run 2: BM25 Disabled ===")
settings.BM25_THRESHOLD = -1.0
pipeline.run("What is the capital of India?", document_ids=None)
