import json
from pathlib import Path

query_path = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/query.json")
with open(query_path, "r", encoding="utf-8") as f:
    queries = json.load(f)

print(f"Total queries in query.json: {len(queries)}")

# Source file breakdown
from collections import Counter
source_counts = Counter(q.get("source_file") for q in queries)
print("\nBreakdown by source_file:")
for src, count in source_counts.items():
    print(f"  - {src}: {count} queries")

# Group queries from non-overlapping documents vs original 3
orig_docs = {"1706.03762v7.pdf", "hdfc-mutual-fund-handbook.pdf", "National-Strategy-for-Artificial-Intelligence.pdf"}
non_overlapping = [q for q in queries if q.get("source_file") not in orig_docs]
overlapping = [q for q in queries if q.get("source_file") in orig_docs]

print(f"\nNon-overlapping queries from NEW documents: {len(non_overlapping)}")
print(f"Queries from original 3 documents: {len(overlapping)}")
