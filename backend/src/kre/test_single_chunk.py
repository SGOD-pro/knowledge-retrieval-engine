import json
from pathlib import Path
from dotenv import load_dotenv
import os
os.environ["ENVIRONMENT"] = "dev"
os.environ["MODEL_PROVIDER"] = "prod"
load_dotenv()
from kre.ingestion.parse_service import parse_file
from kre.providers.embedding_provider import embed_text

def test_single():
    doc_path = Path("../data/policy_regulatory/Newsletter-NITI-SandhanOct-25.pdf")
    document = parse_file(doc_path)
    text = document.chunks[0].text
    
    print("Chunk text:", text[:100])
    
    try:
        emb = embed_text(text, provider="prod")
        print("Embedding returned type:", type(emb))
        if emb is not None:
            print("Length:", len(emb))
    except Exception as e:
        print("Exception during embed_text:", e)

if __name__ == "__main__":
    test_single()
