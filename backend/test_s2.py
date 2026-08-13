import json
from src.services.retrieval.planner import extract_entities, compute_complexity

with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
    gt = json.load(f)

for idx, g in enumerate(gt):
    query = g['query']
    entities = extract_entities(query)
    flags = compute_complexity(query)[1]
    print(f"Q{idx+1}: {query}")
    print(f"Entities ({len(entities)}): {entities}")
    print(f"Flags: {flags}\n")
