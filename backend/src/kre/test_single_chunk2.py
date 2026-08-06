import json
from pathlib import Path
from dotenv import load_dotenv
import os
os.environ["ENVIRONMENT"] = "dev"
os.environ["MODEL_PROVIDER"] = "prod"
load_dotenv()
from kre.ingestion.parse_service import parse_file
from kre.providers.embedding_provider import get_bedrock_client, get_embedding_model

def test_single():
    doc_path = Path("../data/policy_regulatory/Newsletter-NITI-SandhanOct-25.pdf")
    document = parse_file(doc_path)
    text = document.chunks[0].text
    
    print("Chunk text:", text[:100])
    
    client = get_bedrock_client()
    model_id = get_embedding_model("prod")
    response = client.invoke_model(
        modelId=model_id,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({
            "inputText": text[:30000],
            "dimensions": 1024,
            "normalize": True,
        }),
    )
    response_body = json.loads(response.get("body").read())
    print("Response body:", response_body)

if __name__ == "__main__":
    test_single()
