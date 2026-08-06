import os
from pathlib import Path

def replace_in_file(filepath):
    content = filepath.read_text()
    if "kre.shared.providers" in content:
        new_content = content.replace("kre.shared.providers", "kre.providers")
        filepath.write_text(new_content)
        print(f"Updated {filepath}")

src_dir = Path("src")
for root, _, files in os.walk(src_dir):
    for file in files:
        if file.endswith(".py"):
            replace_in_file(Path(root) / file)
