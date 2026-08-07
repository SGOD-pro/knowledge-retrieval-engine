from dotenv import load_dotenv
load_dotenv()
import sys
import os
import json
import dataclasses
from kre.shared.db.postgres import PostgresRepository
from kre.models import Document, Chunk
from kre.providers.embedding_provider import embed_text
from kre.ingestion.embed_service import embed_fast_local

def main():
    repo = PostgresRepository()
    repo.initialize()
    
    benchmark_json = "tests/data/benchmark_queries.json"
    with open(benchmark_json, "r") as f:
        queries = json.load(f)
        
    doc_paths = set()
    for q in queries:
        doc_paths.add(os.path.join(q["folder"], q["document_filename"]))
        
    print(f"Documents to ingest: {doc_paths}")
    print("Fetching chunks from DynamoDB...")
    chunks = repo.get_all_chunks()
    print(f"Found {len(chunks)} chunks in DynamoDB.")
    
    if len(chunks) == 0:
        print("No chunks found in DynamoDB! Cannot re-embed.")
        return
        
    docs_map = {}
    for c in chunks:
        if c.document_id not in docs_map:
            d = repo.get(c.document_id)
            if not d:
                print(f"Could not find document {c.document_id}")
                continue
            docs_map[c.document_id] = d
            
    print(f"Found {len(docs_map)} documents to re-embed.")
    
    total_embedded = 0
    for doc_id, doc in docs_map.items():
        print(f"Processing document {doc.filename} ({len(doc.chunks)} chunks)...")
        new_chunks = []
        for i, chunk in enumerate(doc.chunks):
            emb_fast = chunk.embedding_fast
            emb_full = chunk.embedding_full
            
            try:
                emb_fast = embed_fast_local(chunk.text)
            except Exception as e:
                print(f"Failed to embed fast for chunk {chunk.id}: {e}")
                
            try:
                emb_full = embed_text(chunk.text, provider="prod")
            except Exception as e:
                print(f"Failed to embed full for chunk {chunk.id}: {e}")
            
            # Use dataclasses.replace for frozen dataclass
            new_chunk = dataclasses.replace(chunk, embedding_fast=emb_fast, embedding_full=emb_full)
            new_chunks.append(new_chunk)
            
            if i % 50 == 0:
                print(f"  Embedded {i}/{len(doc.chunks)} chunks...")
                
        # Re-assign chunks
        doc = dataclasses.replace(doc, chunks=tuple(new_chunks))
        print(f"Saving {doc.filename} to Qdrant/DynamoDB...")
        repo.save(doc)
        total_embedded += len(doc.chunks)
        
    print(f"Successfully re-embedded {total_embedded} chunks!")

if __name__ == '__main__':
    main()
