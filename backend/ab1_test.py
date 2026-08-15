import json
from src.services.langgraph_pipeline import pipeline
from run_advance_60_benchmark import _content_match

with open(r'd:\WORK\knowledge-retrieval-engine\backend\tests\data\advance\eval_60_queries.json', 'r', encoding='utf-8') as f:
    eval_queries = json.load(f)

docx_queries = [q for q in eval_queries if q.get('source_file') == 'Workflow Documentation.docx']

hits_full = 0
hits_fast = 0

print('Testing Full Path (Corrected Criteria)...')
for q in docx_queries:
    res = pipeline.run(query=q['query'], force_full_path=True)
    all_chunk_text = ' '.join([c.text for c in getattr(res, 'top_chunks', [])[:5]])
    expected = q.get('expected_answer', '')
    ans = getattr(res, 'answer', '')
    if _content_match(all_chunk_text, expected) or (expected.lower() in ans.lower() and ans != 'NOT_FOUND'):
        hits_full += 1
        print(f"  [FULL] HIT: {q['query']}")
    else:
        print(f"  [FULL] MISS: {q['query']}")

print('Testing Fast Path (Corrected Criteria)...')
for q in docx_queries:
    res = pipeline.run(query=q['query'], force_full_path=False)
    all_chunk_text = ' '.join([c.text for c in getattr(res, 'top_chunks', [])[:5]])
    expected = q.get('expected_answer', '')
    ans = getattr(res, 'answer', '')
    if _content_match(all_chunk_text, expected) or (expected.lower() in ans.lower() and ans != 'NOT_FOUND'):
        hits_fast += 1
        print(f"  [FAST] HIT: {q['query']}")
    else:
        print(f"  [FAST] MISS: {q['query']}")

print(f'\nTrue Full-Path Recall: {hits_full}/{len(docx_queries)}')
print(f'True Fast-Path Recall: {hits_fast}/{len(docx_queries)}')
