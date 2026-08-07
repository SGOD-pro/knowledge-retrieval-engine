import sys
import os
sys.path.append('odl')
os.environ['ENVIRONMENT'] = 'dev'
os.environ['AWS_PROFILE'] = 'aws'
from pathlib import Path
import json

doc_id = "test-doc"
event = {
    "documents": [{
        "document_id": doc_id,
        "s3_bucket": "kre-documents-dev",
        "s3_key": "data/academic_research/1706.03762v7.pdf",
    }]
}
import main
res = main.lambda_handler(event, None)
print(json.dumps(res, indent=2))
