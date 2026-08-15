import json
import asyncio
from pathlib import Path
import sys

# Ensure correct encodings
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from src.ingestion_lambda.parse_service import ingest_document
from src.db.database import CloudRepository
from src.services.langgraph_pipeline import pipeline

doc_path = Path(r'd:\WORK\knowledge-retrieval-engine\backend\tests\data\advance\Workflow Documentation.docx')
doc_id = 'ed987bbc-f76d-4938-a5c4-4b6f374db064'
print('Re-ingesting DOCX...')
ingest_document(doc_path, document_id=doc_id)

repo = CloudRepository()
chunks = repo.get_all_chunks([doc_id])
print(f'New DOCX chunk count: {len(chunks)}')

with open(r'd:\WORK\knowledge-retrieval-engine\backend\tests\data\advance\eval_60_queries.json', 'r', encoding='utf-8') as f:
    gt_data = json.load(f)

docx_queries = [q for q in gt_data if q.get('source_file') == 'Workflow Documentation.docx']

async def evaluate(queries, force_full_path):
    hits = 0
    for q in queries:
        res = pipeline.run(query=q['query'], force_full_path=force_full_path)
        ans = getattr(res, 'answer', '')
        if 'I cannot answer' not in ans and "I don't know" not in ans and 'does not contain' not in ans and len(ans) > 10:
            hits += 1
            print(f'  HIT: {q["query"]} -> {ans[:50]}...')
        else:
            print(f'  MISS: {q["query"]}')
    return hits

async def main():
    print('\nTesting Full-Path First (to see if new chunks fixed the 0% floor)...')
    full_hits = await evaluate(docx_queries, True)
    print(f'Full-Path Recall: {full_hits}/{len(docx_queries)}')

    print('\nTesting Fast-Path (expecting failure as per known limitations)...')
    fp_hits = await evaluate(docx_queries, False)
    print(f'Fast-Path Recall: {fp_hits}/{len(docx_queries)}')

asyncio.run(main())
