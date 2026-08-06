import json
from pathlib import Path
from dotenv import load_dotenv
import os
os.environ["ENVIRONMENT"] = "dev"
os.environ["MODEL_PROVIDER"] = "prod"
load_dotenv()
from kre.ingestion.parse_service import parse_file
from kre.ingestion.embed_service import embed_chunks_dual
from kre.shared.db.postgres import PostgresRepository

def ingest_all():
    repo = PostgresRepository()
    benchmark_json = Path("tests/data/benchmark_queries.json")
    
    with open(benchmark_json, "r", encoding="utf-8") as f:
        queries = json.load(f)
        
    documents_to_ingest = set()
    for q in queries:
        folder = Path("../") / q["folder"]
        filename = q["document_filename"]
        documents_to_ingest.add(folder / filename)
        
    for doc_path in list(documents_to_ingest)[:1]:
        print(f"Ingesting {doc_path}...")
        if not doc_path.exists():
            print(f"Error: {doc_path} does not exist.")
            continue
        from dataclasses import replace
        document = parse_file(doc_path)
        new_chunks = embed_chunks_dual(document.chunks, provider="prod")
        document = replace(document, chunks=tuple(new_chunks))
        print(f"Before save: Chunk 0 full embedding present? {document.chunks[0].embedding_full is not None}")
        repo.save(document)
        print(f"Successfully ingested {doc_path.name} with {len(document.chunks)} chunks.")

if __name__ == "__main__":
    ingest_all()
