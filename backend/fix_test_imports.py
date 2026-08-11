import os
import re

directories_to_scan = [
    r'd:\WORK\knowledge-retrieval-engine\backend\tests',
]

replacements = [
    (re.compile(r'from api\.main import'), r'from main import'),
    (re.compile(r'from retrieval\.'), r'from services.retrieval.'),
    (re.compile(r'from llm\.'), r'from services.llm.'),
    (re.compile(r'from graph\.'), r'from services.'),
]

for directory in directories_to_scan:
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
                print(f"Updated test imports in {filepath}")
