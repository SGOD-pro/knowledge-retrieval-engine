import os
import re

directories_to_scan = [
    r'd:\WORK\knowledge-retrieval-engine\backend\src',
]

def search_files():
    for directory in directories_to_scan:
        for root, dirs, files in os.walk(directory):
            for file in files:
                if not file.endswith('.py'):
                    continue
                filepath = os.path.join(root, file)
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                if 'okf' in content.lower():
                    print(f"Found 'okf' in {filepath}")

search_files()
