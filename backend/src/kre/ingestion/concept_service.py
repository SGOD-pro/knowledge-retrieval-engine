import os
import json
import logging
import re
from kre.shared.models import Chunk
from kre.providers.provider_client import get_active_provider, enforce_rate_limit
from kre.shared.bedrock_models import get_concept_model

logger = logging.getLogger(__name__)

def extract_properties_nova_micro(chunks: list[Chunk], provider: str | None = None) -> list[dict]:
    """
    Tier 3 extraction using Bedrock.
    Processes chunks in batch mode (up to 20 at a time) to stay under budget.
    Expected output: [{concept, property_name, property_value, source_chunk_id, confidence}]
    """
    active = provider or get_active_provider()
    model_id = get_concept_model()
    enforce_rate_limit(model_id)
    results = []
    
    # Process in batches of 20
    batch_size = 20
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i+batch_size]
        
        batch_text = "\n---\n".join([f"[{c.chunk_id}]: {c.text}" for c in batch])
        system_prompt = (
            "You are a strict knowledge extraction system. Extract properties from the provided text chunks.\n"
            "Return a JSON array of objects with the exact schema: "
            '{"concept": "...", "property_name": "...", "property_value": "...", "source_chunk_id": "...", "confidence": 0.0-1.0}'
        )
        user_prompt = f"Extract properties from these chunks:\n{batch_text}"
        
        try:
            from kre.shared.aws import get_client
            client = get_client("bedrock-runtime")
            response = client.converse(
                modelId=model_id,
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
            logger.error("Extraction failed: %s", str(e))
                
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
