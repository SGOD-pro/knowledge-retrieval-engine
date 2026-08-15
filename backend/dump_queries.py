import json

with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
    u_queries = json.load(f)
with open('d:/WORK/knowledge-retrieval-engine/backend/tests/data/advance/eval_60_queries.json', 'r', encoding='utf-8') as f:
    x_queries = json.load(f)

print('--- U QUERIES (17) ---')
for i, q in enumerate(u_queries):
    print(f"{i}: {q['query']}")

print('\n--- X QUERIES (first 15) ---')
for i, q in enumerate(x_queries[:15]):
    print(f"{i}: {q['query']}")
