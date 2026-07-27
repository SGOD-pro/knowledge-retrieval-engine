import os
import json
import logging
import re
from kre.models import Chunk
from kre.providers.provider_client import get_active_provider

logger = logging.getLogger(__name__)

def extract_properties_nova_micro(chunks: list[Chunk], provider: str | None = None) -> list[dict]:
    """
    Tier 3 extraction using amazon.nova-micro-v1 (Prod) or OpenRouter (Dev).
    Processes chunks in batch mode (up to 20 at a time) to stay under budget.
    Expected output: [{concept, property_name, property_value, source_chunk_id, confidence}]
    """
    active = provider or get_active_provider()
    results = []
    
    # Process 20 chunks at a time
    batch_size = 20
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        batch_text = "\n".join(f"[{c.id}]: {c.text}" for c in batch)
        
        system_prompt = (
            "You are a strict data extraction tool. Extract key properties and concepts from the provided chunks. "
            "Return a JSON array of objects with the exact schema: "
            '{"concept": "...", "property_name": "...", "property_value": "...", "source_chunk_id": "...", "confidence": 0.0-1.0}'
        )
        user_prompt = f"Extract properties from these chunks:\n{batch_text}"
        
        if active == "prod":
            try:
                import boto3
                client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
                response = client.converse(
                    modelId="amazon.nova-micro-v1:0",
                    messages=[{"role": "user", "content": [{"text": user_prompt}]}],
                    system=[{"text": system_prompt}],
                    inferenceConfig={"temperature": 0.0}
                )
                output_text = response["output"]["message"]["content"][0]["text"]
                # Strip markdown
                if output_text.startswith("```json"):
                    output_text = output_text[7:]
                elif output_text.startswith("```"):
                    output_text = output_text[3:]
                if output_text.endswith("```"):
                    output_text = output_text[:-3]
                output_text = output_text.strip()
                
                extracted = json.loads(output_text)
                if isinstance(extracted, list):
                    results.extend(extracted)
            except Exception as e:
                logger.error("Nova Micro extraction failed: %s", str(e))
        elif active == "dev" and os.environ.get("OPENROUTER_API_KEY"):
            try:
                import requests
                response = requests.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "nvidia/nemotron-3-nano-30b-a3b:free",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "temperature": 0.0,
                    },
                    timeout=30.0,
                )
                if response.status_code == 200:
                    output_text = response.json()["choices"][0]["message"]["content"]
                    if output_text.startswith("```json"):
                        output_text = output_text[7:]
                    elif output_text.startswith("```"):
                        output_text = output_text[3:]
                    if output_text.endswith("```"):
                        output_text = output_text[:-3]
                    output_text = output_text.strip()
                    
                    extracted = json.loads(output_text)
                    if isinstance(extracted, list):
                        results.extend(extracted)
            except Exception as e:
                logger.error("Dev extraction failed: %s", str(e))
                
    return results

def extract_tier1_patterns(chunks: list[Chunk]) -> list[dict]:
    """
    Tier 1 extraction using strict Regex for dates, percentages, and identifiers.
    """
    results = []
    # Dates: yyyy-mm-dd or mm/dd/yyyy
    date_pattern = re.compile(r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})\b')
    # Percentages: numbers followed by %
    pct_pattern = re.compile(r'\b(\d+(?:\.\d+)?)%\b')
    # Identifiers: Alphanumeric ending with uppercase/numbers
    id_pattern = re.compile(r'\b([A-Z0-9]{5,})\b')
    
    for c in chunks:
        for match in date_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Date",
                "property_value": match.group(1),
                "source_chunk_id": c.id,
                "confidence": 1.0
            })
        for match in pct_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Percentage",
                "property_value": match.group(1) + "%",
                "source_chunk_id": c.id,
                "confidence": 1.0
            })
        for match in id_pattern.finditer(c.text):
            results.append({
                "concept": "DocumentEntity",
                "property_name": "Identifier",
                "property_value": match.group(1),
                "source_chunk_id": c.id,
                "confidence": 1.0
            })
    return results
