import os
import re

directories_to_scan = [
    r'd:\WORK\knowledge-retrieval-engine\backend\tests',
]

replacements = [
    (re.compile(r'patch\("api\.main\.'), r'patch("main.'),
    (re.compile(r'patch\("retrieval\.'), r'patch("services.retrieval.'),
    (re.compile(r'patch\("llm\.'), r'patch("services.llm.'),
    (re.compile(r'patch\("graph\.'), r'patch("services.'),
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
                print(f"Updated patch strings in {filepath}")
