import json
import sys
from pathlib import Path

if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

from config import settings
from db.database import CloudRepository
from services.langgraph_pipeline import pipeline
from run_benchmark import _parse_page, _expected_pages

repo = CloudRepository()

with open('llm_ground_truths.json', 'r', encoding='utf-8') as f:
    gt17 = json.load(f)

# Pick 5 queries across different documents
test_indices = [0, 1, 2, 3, 4]  # Q01 (National Strategy), Q02 (National Strategy), Q03 (National Strategy), Q04 (Bills/Govt), Q05 (Transformer)

print("=" * 95)
print("=== AG1 & AG2: IN-DEPTH ROOT CAUSE TRACE FOR 5 BASELINE MISSES ===")
print("=" * 95)

for idx in test_indices:
    gt = gt17[idx]
    qid = f"BASE_{idx+1:02d}"
    query = gt["query"]
    citations = gt.get("citations", [])
    expected_ans = gt.get("llm_answer", "")
    exp_pages = _expected_pages(gt)
    
    first_cit = citations[0] if citations else {}
    gt_doc_id = first_cit.get("document_id")
    gt_chunk_id = first_cit.get("chunk_id")
    gt_page = _parse_page(first_cit)
    
    print(f"\n" + "-" * 95)
    print(f"QUERY [{qid}]: {query}")
    print(f"-" * 95)
    print(f"1. GROUND TRUTH METADATA:")
    print(f"   - Expected Chunk ID:      {gt_chunk_id}")
    print(f"   - Expected Doc ID:        {gt_doc_id}")
    print(f"   - Expected Page(s):       {exp_pages} (parsed from {first_cit.get('location_reference')})")
    print(f"   - Expected Answer:        {expected_ans}")
    
    # 2. Direct ID Lookup in Current DB (DynamoDB + Qdrant)
    # Check DynamoDB for old doc_id
    ddb_doc = repo.table.get_item(Key={"PK": f"DOC#{gt_doc_id}", "SK": f"DOC#{gt_doc_id}"}).get("Item")
    # Check DynamoDB for old chunk_id
    ddb_chunk = repo.table.get_item(Key={"PK": f"DOC#{gt_doc_id}", "SK": f"CHUNK#{gt_chunk_id}"}).get("Item") if gt_chunk_id else None
    
    print(f"\n2. DIRECT ID LOOKUP IN FRESHLY RE-INGESTED DATABASE:")
    print(f"   - GT Doc ID in DynamoDB:   {'EXISTS' if ddb_doc else 'DOES NOT EXIST (Re-ingestion assigned new UUID)'}")
    print(f"   - GT Chunk ID in DynamoDB: {'EXISTS' if ddb_chunk else 'DOES NOT EXIST (Chunk ID prefix changed with new Doc UUID)'}")
    
    # 3. Live Pipeline Execution Trace
    res = pipeline.run(query)
    top_chunks = getattr(res, "top_chunks", [])
    actual_ans = getattr(res, "answer", "")
    path_used = "FAST" if getattr(res, "fast_path", False) else "FULL"
    context = getattr(res, "context_snippet", "")
    
    retrieved_pages = [c.page_number for c in top_chunks[:5] if c.page_number is not None]
    page_match = any(p in exp_pages for p in retrieved_pages)
    
    print(f"\n3. LIVE PIPELINE RETRIEVAL TRACE:")
    print(f"   - Routing Path:           {path_used}")
    print(f"   - Retrieved Top-5 Pages:  {retrieved_pages}")
    print(f"   - Page Match (Hit@5):     {'YES (Page ' + str(exp_pages) + ' found!)' if page_match else 'NO'}")
    print(f"   - Retrieved Chunks:")
    for rank, c in enumerate(top_chunks[:5], 1):
        is_target_page = c.page_number in exp_pages if exp_pages else False
        marker = " [TARGET PAGE!]" if is_target_page else ""
        print(f"       [{rank}] Doc: {c.document_id[:8]}.. | Page: {c.page_number:<3}{marker} | Text: {c.text[:90].replace(chr(10), ' ')}...")
        
    print(f"\n4. ANSWER TEXT COMPARISON (AG2 SUBSTANTIVE ACCURACY):")
    print(f"   [EXPECTED GROUND TRUTH]:\n   \"{expected_ans}\"")
    print(f"\n   [ACTUAL PIPELINE ANSWER]:\n   \"{actual_ans}\"")
    
print("\n" + "=" * 95)
