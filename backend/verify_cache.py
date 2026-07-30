import json
import time
import uuid
import os

# Ensure REDIS_URL is loaded for the test before imports happen
if not os.environ.get("REDIS_URL"):
    os.environ["REDIS_URL"] = "redis://localhost:6379/0"

from kre.query_lambda.api.main import query_endpoint, QueryRequest
from kre.shared.db.redis_cache import cache

def test_cache():
    # Use a highly specific query to ensure a fresh cache key
    unique_id = str(uuid.uuid4())
    test_query = f"What is the capital of France? {unique_id}"
    
    req = QueryRequest(query=test_query, document_ids=[])

    print("\n--- Request 1: Fresh Query ---")
    t0 = time.perf_counter()
    resp1 = query_endpoint(req)
    t1 = time.perf_counter()
    print(f"Time: {(t1 - t0)*1000:.2f}ms")
    print(f"Cached: {resp1.get('cached')}")
    print(f"Answer: {resp1.get('answer')}")

    print("\n--- Request 2: Cached Query ---")
    t2 = time.perf_counter()
    resp2 = query_endpoint(req)
    t3 = time.perf_counter()
    print(f"Time: {(t3 - t2)*1000:.2f}ms")
    print(f"Cached: {resp2.get('cached')}")
    print(f"Answer: {resp2.get('answer')}")

    print("\n--- Request 3: NOT_FOUND Query ---")
    # Forcing a NOT_FOUND query (highly dependent on the pipeline, but assuming a garbage query yields NOT_FOUND)
    # Actually, the best way to test NOT_FOUND caching is to manually inject a NOT_FOUND payload into the pipeline response 
    # or craft a query we know fails to return a result.
    bad_query = f"asdfjasdlkfjasdlkfjasdf {unique_id}"
    req_bad = QueryRequest(query=bad_query, document_ids=[])
    
    resp_bad_1 = query_endpoint(req_bad)
    resp_bad_2 = query_endpoint(req_bad)
    
    print(f"Bad Query Answer: {resp_bad_1.get('answer')}")
    print(f"Bad Query Cached on Request 1: {resp_bad_1.get('cached')}")
    print(f"Bad Query Cached on Request 2: {resp_bad_2.get('cached')}")
    
    if not resp1.get('cached') and resp2.get('cached'):
        print("\n✅ SUCCESS: Cache hit on second request!")
    else:
        print("\n❌ FAILURE: Cache did not hit on second request.")
        
    if not resp_bad_2.get('cached'):
        print("✅ SUCCESS: NOT_FOUND was NOT cached!")
    else:
        print("❌ FAILURE: NOT_FOUND was incorrectly cached.")

if __name__ == "__main__":
    test_cache()
