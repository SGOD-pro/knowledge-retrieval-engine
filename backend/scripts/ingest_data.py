import os
import sys
import httpx
from pathlib import Path

BACKEND_URL = "http://127.0.0.1:8000/ingest"
DATA_DIR = Path(__file__).parent.parent / "data"

def main():
    if not DATA_DIR.exists():
        print(f"Data directory not found at {DATA_DIR}")
        sys.exit(1)

    print(f"Starting ingestion from {DATA_DIR}...")
    success_count = 0
    fail_count = 0

    with httpx.Client(timeout=300.0) as client:
        # Traverse all subdirectories and files
        for root, _, files in os.walk(DATA_DIR):
            for file in files:
                # Skip hidden files or non-supported formats just in case, though backend will validate
                if file.startswith('.') or not file.lower().endswith(('.pdf', '.docx', '.xlsx', '.pptx', '.csv')):
                    continue
                
                file_path = Path(root) / file
                print(f"Ingesting {file_path.name}...", end=" ", flush=True)
                
                try:
                    with open(file_path, "rb") as f:
                        response = client.post(BACKEND_URL, files={"file": (file_path.name, f)})
                    
                    if response.status_code == 200:
                        data = response.json()
                        print(f"SUCCESS (Chunks: {data.get('chunk_count', 0)})")
                        success_count += 1
                    else:
                        print(f"FAILED ({response.status_code}: {response.text})")
                        fail_count += 1
                except Exception as e:
                    print(f"ERROR: {str(e)}")
                    fail_count += 1

    print(f"\nIngestion Complete. Success: {success_count}, Failed: {fail_count}")

if __name__ == "__main__":
    main()
