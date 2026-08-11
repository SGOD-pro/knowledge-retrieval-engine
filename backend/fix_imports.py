import os
import re

directories_to_scan = [
    r'd:\WORK\knowledge-retrieval-engine\backend\src',
    r'd:\WORK\knowledge-retrieval-engine\backend\tests',
    r'd:\WORK\knowledge-retrieval-engine\backend\ingestion_lambda'
]

replacements = [
    (re.compile(r'from query_lambda\.api\.main import'), r'from main import'),
    (re.compile(r'from query_lambda\.retrieval\.'), r'from services.retrieval.'),
    (re.compile(r'from query_lambda\.llm\.'), r'from services.llm.'),
    (re.compile(r'from query_lambda\.graph\.'), r'from services.'),
    (re.compile(r'from shared\.config import'), r'from config import'),
    (re.compile(r'from shared\.models import'), r'from schemas.models import'),
    (re.compile(r'from shared\.aws import'), r'from aws.infra import'),
    (re.compile(r'from shared\.bedrock_models import'), r'from providers.bedrock_models import'),
    (re.compile(r'from shared\.db\.'), r'from db.'),
    (re.compile(r'import shared\.config'), r'import config'),
    (re.compile(r'import query_lambda\.'), r'import services.'),
]

for directory in directories_to_scan:
    if not os.path.exists(directory):
        continue
    for root, dirs, files in os.walk(directory):
        for file in files:
            if not file.endswith('.py'):
                continue
            filepath = os.path.join(root, file)
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            
            new_content = content
            for pattern, repl in replacements:
                new_content = pattern.sub(repl, new_content)
                
            if new_content != content:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(new_content)
                print(f"Updated imports in {filepath}")
