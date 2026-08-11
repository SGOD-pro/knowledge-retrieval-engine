# BGE-Small Embedding Microservice

Standalone FastAPI service that runs **BGE-small-en-v1.5** via ONNX Runtime.

## Setup

1. Place your `model.onnx` file (BGE-small-en-v1.5 exported to ONNX) in this directory.
2. Optionally place `tokenizer.json` here for offline tokenization.
3. Install dependencies:

```bash
pip install fastapi uvicorn onnxruntime numpy tokenizers
```

4. Run:

```bash
uvicorn main:app --port 8001
```

## Endpoints

### `POST /embed`
Embed a single text into a 384-dim vector.

```json
// Request
{"text": "What is the refund policy?"}

// Response
{"embedding": [0.123, -0.456, ...], "dim": 384}
```

### `POST /embed/batch`
Embed multiple texts.

```json
// Request
{"texts": ["text1", "text2"]}

// Response
{"embeddings": [[...], [...]], "dim": 384}
```

### `GET /health`
Health check — reports whether the model is loaded.

## Integration

The KRE Query Lambda's `embedding_provider.py` calls this microservice for fast-path queries:

```python
BGE_MICROSERVICE_URL = os.environ.get("BGE_MICROSERVICE_URL", "http://localhost:8001")
response = requests.post(f"{BGE_MICROSERVICE_URL}/embed", json={"text": query})
embedding = response.json()["embedding"]  # 384-dim vector
```

## Deployment Options

- **Local dev**: `uvicorn main:app --port 8001`
- **AWS Lambda**: Wrap with Mangum, deploy as container image (ONNX weights ~130MB)
- **ECS/Fargate**: Docker container for production scaling
