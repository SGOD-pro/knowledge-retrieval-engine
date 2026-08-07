import sys, os
sys.path.append('backend/src')
os.environ['ENVIRONMENT'] = 'dev'
os.environ['AWS_PROFILE'] = 'aws'
from kre.ingestion_lambda.adapters.pdf_adapter import parse
from pathlib import Path
res = parse(Path('data/academic_research/1706.03762v7.pdf'), 'test-doc')
print(f"Chunks: {len(res)}")
