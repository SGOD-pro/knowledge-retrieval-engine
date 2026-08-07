import os
from qdrant_client import QdrantClient
from qdrant_client.http import models
from dotenv import load_dotenv

load_dotenv()

qclient = QdrantClient(url=os.environ.get("QDRANT_URL"), api_key=os.environ.get("QDRANT_API_KEY"))
collection_name = "kre_chunks"

print(f"Creating payload indexes for {collection_name}...")

try:
    qclient.create_payload_index(
        collection_name=collection_name,
        field_name="original_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    print("Created index for 'original_id'")
except Exception as e:
    print(f"Index original_id: {e}")

try:
    qclient.create_payload_index(
        collection_name=collection_name,
        field_name="document_id",
        field_schema=models.PayloadSchemaType.KEYWORD,
    )
    print("Created index for 'document_id'")
except Exception as e:
    print(f"Index document_id: {e}")

try:
    qclient.create_payload_index(
        collection_name=collection_name,
        field_name="page_number",
        field_schema=models.PayloadSchemaType.INTEGER,
    )
    print("Created index for 'page_number'")
except Exception as e:
    print(f"Index page_number: {e}")

print("Done indexing.")
