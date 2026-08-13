import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from src.providers.reranker_provider import nvidia_nim_reranker

def test_nim():
    print("Testing NVIDIA NIM Reranker...")
    query = "What is the capital of France?"
    documents = ["Paris is the capital of France.", "The sky is blue."]
    
    try:
        scores = nvidia_nim_reranker(query, documents)
        print(f"SUCCESS: {scores}")
    except Exception as e:
        print(f"FAILED: {e}")

if __name__ == "__main__":
    test_nim()
