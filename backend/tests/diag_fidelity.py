import sys
import os
import logging
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO)

from services.retrieval.fidelity_check import check_fidelity

QUERY = "What are the key financial metrics mentioned in the document?"
# This is the actual text from submission.pptx that contains the answer
COMPRESSED = "Citizens lack digital literacy, face English-only portals, and receive no guidance before office visits — leading to wasted time, money, and rejected applications. \u20b990-130 travel costs wasted per failed visit."

print(f"Testing Semantic Fidelity Check...")
print(f"Query: {QUERY}")

try:
    coverage = check_fidelity(QUERY, COMPRESSED)
    print(f"\nRESULT: PASSED (Coverage: {coverage:.2f})")
except Exception as e:
    print(f"\nRESULT: FAILED ({e})")
