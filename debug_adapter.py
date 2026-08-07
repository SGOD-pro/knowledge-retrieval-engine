import sys, os
sys.path.append('backend/src')
os.environ['ENVIRONMENT'] = 'dev'
os.environ['AWS_PROFILE'] = 'aws'
from kre.ingestion_lambda.adapters.pdf_adapter import parse
from pathlib import Path

# Mock odl_main
import sys
import types
odl_main_mock = types.ModuleType("main")
odl_main_mock.lambda_handler = lambda event, context: {
    "results": [{
        "document_id": "test-doc",
        "elements": {
            "text": [{"type": "paragraph", "content": "hello world"}]
        }
    }],
    "failed": []
}
sys.modules['main'] = odl_main_mock

res = parse(Path('dummy.pdf'), 'test-doc')
print(f"Chunks: {len(res)}")
if res:
    print(res[0])
