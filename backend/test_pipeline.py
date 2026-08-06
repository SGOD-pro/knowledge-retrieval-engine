from kre.query_lambda.graph.langgraph_pipeline import pipeline
state = {'query': 'Who are the primary NITI Aayog authors credited with writing the National Strategy for Artificial Intelligence report?', 'document_ids': None}
from kre.query_lambda.graph.langgraph_pipeline import route_query, run_bm25, run_page_index, run_vector
state.update(route_query(state))
state.update(run_bm25(state))
state.update(run_page_index(state))
print(f'Candidate Pages: {state.get("candidate_page_ids")}')
state.update(run_vector(state))
print(f'Top chunks after vector: {[c.id for c in state.get("candidate_chunks", [])]}')
