import json
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()
from kre.ingestion.parse_service import parse_file
from kre.ingestion.embed_service import embed_chunks_dual
from kre.db.postgres import PostgresRepository

def ingest_all():
    repo = PostgresRepository()
    benchmark_json = Path("tests/data/benchmark_queries.json")
    
    with open(benchmark_json, "r", encoding="utf-8") as f:
        queries = json.load(f)
        
    documents_to_ingest = set()
    for q in queries:
        # e.g., "data/policy_regulatory"
        folder = Path("../") / q["folder"]
        filename = q["document_filename"]
        documents_to_ingest.add(folder / filename)
        
    for doc_path in documents_to_ingest:
        print(f"Ingesting {doc_path}...")
        if not doc_path.exists():
            print(f"Error: {doc_path} does not exist.")
            continue
        try:
            from dataclasses import replace
            document = parse_file(doc_path)
            # Compute embeddings
            new_chunks = embed_chunks_dual(document.chunks, provider="dev")
            document = replace(document, chunks=tuple(new_chunks))
            repo.save(document)
            print(f"Successfully ingested {doc_path.name} with {len(document.chunks)} chunks.")
        except Exception as e:
            print(f"Failed to ingest {doc_path.name}: {e}")

if __name__ == "__main__":
    ingest_all()
