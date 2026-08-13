import json
from services.retrieval.planner import compute_complexity

def main():
    with open('d:/WORK/knowledge-retrieval-engine/backend/llm_ground_truths.json', 'r', encoding='utf-8') as f:
        ground_truths = json.load(f)
    
    for i, gt in enumerate(ground_truths):
        query = gt["query"]
        score, flags = compute_complexity(query)
        print(f"Q{i+1}: score={score:.2f}, flags={flags}")

if __name__ == "__main__":
    main()
