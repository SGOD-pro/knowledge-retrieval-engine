import time
from dotenv import load_dotenv; load_dotenv(".env")
import logging
logging.basicConfig(level=logging.DEBUG)

from kre.api.main import query_endpoint, QueryRequest
req = QueryRequest(query="What is Turner supported by?")

print("Sending query request...")
t0 = time.time()
res = query_endpoint(req)
print(f"Time: {time.time()-t0}")
print("Response:", res)
