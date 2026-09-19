import logging
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

backend_dir = Path(__file__).resolve().parent.parent
load_dotenv(backend_dir / ".env")
sys.path.insert(0, str(backend_dir / "src"))

from config import settings
from db.database import CloudRepository
from qdrant_client.http import models as qmodels

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reset_db")


def reset_all():
    print("=" * 70)
    print("STARTING FULL REPOSITORY, DYNAMODB, QDRANT, AND DUMP CLEANUP")
    print("=" * 70)

    repo = CloudRepository()
    table = repo.table

    # 1. Clear DynamoDB table completely
    print("\n1. Scanning and deleting all items from DynamoDB table 'kre-table'...")
    deleted_items = 0
    while True:
        resp = table.scan(ProjectionExpression="PK, SK")
        items = resp.get("Items", [])
        if not items:
            break
        with table.batch_writer() as batch:
            for it in items:
                batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
                deleted_items += 1
        print(f"   Deleted batch of {len(items)} items (total: {deleted_items})...")
        if "LastEvaluatedKey" not in resp:
            break

    print(f"DynamoDB cleanup complete. Total items deleted: {deleted_items}")

    # 2. Reset Qdrant collection
    print(f"\n2. Recreating Qdrant collection '{repo.collection_name}'...")
    try:
        if repo.qclient.collection_exists(repo.collection_name):
            repo.qclient.delete_collection(repo.collection_name)
            print(f"   Old collection '{repo.collection_name}' deleted.")

        repo.qclient.create_collection(
            collection_name=repo.collection_name,
            vectors_config={
                "embedding_fast": qmodels.VectorParams(
                    size=384, distance=qmodels.Distance.COSINE
                ),
                "embedding_full": qmodels.VectorParams(
                    size=1024, distance=qmodels.Distance.COSINE
                ),
            },
        )
        print(f"   Collection '{repo.collection_name}' created.")

        for field_name, schema in [
            ("page_number", qmodels.PayloadSchemaType.INTEGER),
            ("document_id", qmodels.PayloadSchemaType.KEYWORD),
            ("original_id", qmodels.PayloadSchemaType.KEYWORD),
            ("workspace_id", qmodels.PayloadSchemaType.KEYWORD),
        ]:
            repo.qclient.create_payload_index(
                collection_name=repo.collection_name,
                field_name=field_name,
                field_schema=schema,
            )
        print("   Payload indexes recreated.")
    except Exception as e:
        print(f"   Qdrant error: {e}")

    # 3. Clean local tmp dump files
    print("\n3. Cleaning temporary benchmark result dumps in backend/tmp...")
    tmp_dir = backend_dir / "tmp"
    cleaned_files = 0
    patterns = ["*benchmark*.json", "*baseline*.json", "*.bak.json", "output.txt", "ingest.log"]
    for pat in patterns:
        for f in tmp_dir.glob(pat):
            try:
                f.unlink()
                cleaned_files += 1
            except Exception as e:
                print(f"   Could not remove {f.name}: {e}")
    print(f"Cleaned {cleaned_files} benchmark dump files from backend/tmp.")

    # 4. Verify clean state
    scan_after = table.scan(Select="COUNT")
    q_info = repo.qclient.get_collection(repo.collection_name)
    print("\n" + "=" * 70)
    print("VERIFICATION OF CLEAN REPOSITORY STATE:")
    print(f"   DynamoDB Items: {scan_after.get('Count', 0)}")
    print(f"   Qdrant Points:  {q_info.points_count}")
    print("=" * 70)


if __name__ == "__main__":
    reset_all()
