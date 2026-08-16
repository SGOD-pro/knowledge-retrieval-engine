import sys
from collections import defaultdict
from pathlib import Path

# Ensure src is on python path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from db.database import CloudRepository

repo = CloudRepository()

# Map document_id -> filename
scan_resp = repo.table.scan()
items = scan_resp.get("Items", [])
while "LastEvaluatedKey" in scan_resp:
    scan_resp = repo.table.scan(ExclusiveStartKey=scan_resp["LastEvaluatedKey"])
    items.extend(scan_resp.get("Items", []))

doc_map = {}
for item in items:
    if item["PK"].startswith("DOC#") and item["SK"].startswith("DOC#"):
        doc_map[item["id"]] = item.get("filename", "Unknown")

print(f"Discovered {len(doc_map)} active documents in DynamoDB:")
for doc_id, fname in doc_map.items():
    print(f"  - [{doc_id}] {fname}")

print("\nAuditing 100% of Qdrant points with vectors...")

stats = defaultdict(
    lambda: {
        "total": 0,
        "fast_ok": 0,
        "full_ok": 0,
        "null_fast": 0,
        "null_full": 0,
        "zero_padded_full": 0,
        "dim_mismatch": 0,
    }
)

total_points = 0
offset = None

while True:
    res, offset = repo.qclient.scroll(
        collection_name=repo.collection_name,
        limit=500,
        with_vectors=True,
        offset=offset,
    )
    for pt in res:
        total_points += 1
        doc_id = pt.payload.get("document_id", "Unknown")
        fname = doc_map.get(doc_id, f"Doc_{doc_id}")

        doc_stat = stats[fname]
        doc_stat["total"] += 1

        # Check fast vector
        v_fast = pt.vector.get("embedding_fast")
        if v_fast is None:
            doc_stat["null_fast"] += 1
        elif len(v_fast) != 384 or all(x == 0.0 for x in v_fast):
            doc_stat["dim_mismatch"] += 1
        else:
            doc_stat["fast_ok"] += 1

        # Check full vector
        v_full = pt.vector.get("embedding_full")
        if v_full is None:
            doc_stat["null_full"] += 1
        elif len(v_full) != 1024 or all(x == 0.0 for x in v_full):
            doc_stat["dim_mismatch"] += 1
        elif all(x == 0.0 for x in v_full[384:]):
            # This is the exact signature of the old zero-pad hack!
            doc_stat["zero_padded_full"] += 1
        else:
            doc_stat["full_ok"] += 1

    if offset is None:
        break

print("\n" + "=" * 105)
print(
    f"{'DOCUMENT FILENAME':<50} | {'TOTAL':<6} | {'FAST OK':<8} | {'FULL OK':<8} | {'NULL':<6} | {'ZERO-PAD':<8}"
)
print("=" * 105)

total_chunks = 0
total_fast_ok = 0
total_full_ok = 0
total_null = 0
total_zero_padded = 0

for fname, s in sorted(stats.items()):
    total_chunks += s["total"]
    total_fast_ok += s["fast_ok"]
    total_full_ok += s["full_ok"]
    total_null += s["null_fast"] + s["null_full"]
    total_zero_padded += s["zero_padded_full"]

    print(
        f"{fname:<50} | {s['total']:<6} | {s['fast_ok']:<8} | {s['full_ok']:<8} | {s['null_fast'] + s['null_full']:<6} | {s['zero_padded_full']:<8}"
    )

print("=" * 105)
print(
    f"{'TOTAL AUDITED':<50} | {total_chunks:<6} | {total_fast_ok:<8} | {total_full_ok:<8} | {total_null:<6} | {total_zero_padded:<8}"
)
print("=" * 105)

if (
    total_null == 0
    and total_zero_padded == 0
    and total_chunks > 0
    and total_fast_ok == total_chunks
    and total_full_ok == total_chunks
):
    print(
        "\n>>> AUDIT PASSED: ZERO NULLS, ZERO ZERO-PADS, 100% AUTHENTIC 384/1024 DUAL EMBEDDINGS. <<<"
    )
else:
    print("\n>>> AUDIT FAILED! CORRUPTED OR INCOMPLETE EMBEDDINGS DETECTED. <<<")
    sys.exit(1)
