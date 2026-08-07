import os
from qdrant_client import QdrantClient
from dotenv import load_dotenv

load_dotenv()

qclient = QdrantClient(url=os.environ.get("QDRANT_URL", "http://localhost:6333"), api_key=os.environ.get("QDRANT_API_KEY"))

results = qclient.scroll(
    collection_name="kre_chunks",
    limit=5,
    with_vectors=True
)

points = results[0]
if not points:
    print("Qdrant collection is empty!")
else:
    for p in points:
        print(f"Point ID: {p.id}, Payload: {p.payload}")
        if p.vector:
            print(f"  Vectors available: {list(p.vector.keys())}")
        else:
            print("  No vectors!")
