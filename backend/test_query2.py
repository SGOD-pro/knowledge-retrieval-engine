import json
from dotenv import load_dotenv
load_dotenv()
from kre.graph.langgraph_pipeline import pipeline

with open("tests/data/benchmark_queries.json") as f:
    queries = json.load(f)

q = queries[1] # Q002
print(f"Query: {q['query']}")
print(f"Expected Page: {q['source_page']}")

res = pipeline.run(q["query"])
chunks = res.top_chunks

for i, c in enumerate(chunks):
    print(f"Rank {i+1}: chunk_id={c}")
