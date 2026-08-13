import json
from pathlib import Path

query_path = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/query.json")
with open(query_path, "r", encoding="utf-8") as f:
    all_queries = json.load(f)

# The 6 new disjoint documents
TARGET_DOCS = [
    "Govt_Colleges_TeachingStaff_Position_2024_25_0.csv",
    "rs_Bills_Passed_Returned_from_session_217-241.csv",
    "submission.pptx",
    "Workflow Documentation.docx",
    "2204.13154v1.pdf",
    "2507.19595v3.pdf",
]

selected_queries = []
per_doc_counts = {}

for doc in TARGET_DOCS:
    doc_queries = [q for q in all_queries if q.get("source_file") == doc]
    # Take up to 10 queries per document
    chosen = doc_queries[:10]
    selected_queries.extend(chosen)
    per_doc_counts[doc] = len(chosen)

print(f"Selected {len(selected_queries)} non-overlapping queries across 6 new documents:")
for doc, cnt in per_doc_counts.items():
    print(f"  - {doc}: {cnt} queries")

out_path = Path("d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/eval_60_queries.json")
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(selected_queries, f, indent=2)

print(f"\nSaved 60-query benchmark dataset to: {out_path}")
