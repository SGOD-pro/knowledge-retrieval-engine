import unittest.mock as mock
from src.services.langgraph_pipeline import pipeline

q = "Why is the dot product scaled by 1/sqrt(d_k) in Scaled Dot-Product Attention as key dimension d_k grows large?"

print("Running FAST PATH...")
with mock.patch('src.services.retrieval.planner.planner.route') as patch_fast:
    patch_fast.return_value = mock.Mock(fast_path=True, use_graph=False)
    fast = pipeline.run(q).answer

print("Running FULL PATH...")
with mock.patch('src.services.retrieval.planner.planner.route') as patch_full:
    patch_full.return_value = mock.Mock(fast_path=False, use_graph=False)
    full = pipeline.run(q).answer

print(f'\n--- FAST PATH ---\n{fast}\n\n--- FULL PATH ---\n{full}')
