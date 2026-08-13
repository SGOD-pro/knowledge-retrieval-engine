import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
from src.config import settings
from qdrant_client import QdrantClient

client = QdrantClient(url=settings.QDRANT_URL, api_key=settings.QDRANT_API_KEY)
collections = client.get_collections()
print("Collections:", collections)

try:
    info = client.get_collection("chunks")
    print(f"Chunks collection points: {info.points_count}")
except Exception as e:
    print("Chunks collection error:", e)
