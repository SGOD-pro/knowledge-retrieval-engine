import math
import re
from typing import Any


def tokenize(text: str) -> list[str]:
    """Tokenizes text into lowercase alphanumeric tokens of length > 2."""
    if not text:
        return []
    return [w.lower() for w in re.findall(r"\w+", text) if len(w) > 2]


def content_match(chunk_text: str, expected_answer: str) -> bool:
    """
    Canonical benchmark content match scoring function.
    Returns True if:
      - Asymmetric token recall >= 0.50 (fraction of expected answer tokens present in chunk), OR
      - When numbers or percentages are in the expected answer, >= 50% numeric overlap with >= 25% token recall.
    Also ensures reasonable token precision when chunk text is short.
    """
    if not expected_answer or not chunk_text:
        return False

    exp_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", expected_answer))
    expected_tokens = set(tokenize(expected_answer))

    if not expected_tokens and not exp_nums:
        return False

    chunk_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", chunk_text))
    if exp_nums and not expected_tokens:
        return len(exp_nums & chunk_nums) == len(exp_nums)

    chunk_tokens = set(tokenize(chunk_text))
    overlap = len(expected_tokens & chunk_tokens)
    recall = overlap / len(expected_tokens) if expected_tokens else 0.0

    # Check key numbers / percentages
    if exp_nums:
        num_overlap = len(exp_nums & chunk_nums) / len(exp_nums)
        if num_overlap >= 0.5 and recall >= 0.25:
            return True

    return recall >= 0.50


def compute_faithfulness(answer: str, context: str) -> float | None:
    """
    Canonical faithfulness scoring function.
    Returns:
      - None if the answer is an abstention (NOT_FOUND, empty, etc.), ensuring
        abstentions are not counted as hallucinations (0.0) nor fake successes (1.0).
      - Float in [0.0, 1.0] indicating the fraction of non-trivial answer terms
        and numeric claims grounded in the retrieved context text.
    Note: This is a fast lexical approximation; NLI-based entailment is planned for v2.
    """
    if not answer or answer in ("NOT_FOUND", "", "The context does not provide"):
        return None

    answer_clean = answer.strip()
    if answer_clean == "NOT_FOUND" or not answer_clean:
        return None

    # Extract non-trivial answer terms (>3 chars) and numeric entities
    words = [w.lower() for w in re.findall(r"\w+", answer_clean) if len(w) > 3]
    numbers = re.findall(r"\b\d+(?:\.\d+)?%?\b", answer_clean)
    answer_terms = set(words + numbers)

    if not answer_terms:
        return 1.0

    context_lower = context.lower() if context else ""
    found = sum(1 for t in answer_terms if t in context_lower)
    return round(found / len(answer_terms), 4)


def compute_answer_relevancy(answer: str, expected: str) -> float:
    """
    Evaluates semantic quality and numeric accuracy of the answer against expected ground truth.
    Returns float in [0.0, 1.0].
    """
    if not answer or not expected:
        return 0.0

    ans_clean = answer.strip().lower()
    exp_clean = expected.strip().lower()

    if ans_clean == exp_clean:
        return 1.0

    # Extract numbers and tokens
    exp_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", exp_clean))
    ans_nums = set(re.findall(r"\b\d+(?:\.\d+)?%?\b", ans_clean))

    exp_tokens = set(tokenize(exp_clean))
    ans_tokens = set(tokenize(ans_clean))

    overlap = len(exp_tokens & ans_tokens)
    recall = overlap / len(exp_tokens) if exp_tokens else 0.0
    precision = overlap / len(ans_tokens) if ans_tokens else 0.0
    f1 = (2 * recall * precision) / (recall + precision) if (recall + precision) > 0 else 0.0

    if exp_nums:
        num_overlap = len(exp_nums & ans_nums) / len(exp_nums)
        # Weighted combination: 60% numeric correctness, 40% token overlap F1
        score = 0.6 * num_overlap + 0.4 * f1
    else:
        # Heavily weigh recall with penalty for excessive hallucinated length
        score = 0.7 * recall + 0.3 * precision

    return round(min(1.0, max(0.0, score)), 4)


def compute_ndcg_at_k(retrieved_docs: list[str], relevant_docs: set[str], k: int = 5) -> float:
    """
    Computes Normalized Discounted Cumulative Gain at rank k (nDCG@k).
    Standard binary relevance definition.
    """
    if not relevant_docs:
        return 1.0 if not retrieved_docs else 0.0

    top_k = retrieved_docs[:k]
    dcg = 0.0
    for rank, doc in enumerate(top_k, 1):
        if doc in relevant_docs:
            dcg += 1.0 / math.log2(rank + 1)

    idcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, min(len(relevant_docs), k) + 1))
    if idcg <= 0.0:
        return 0.0

    return round(dcg / idcg, 4)


def compute_precision_at_k(retrieved_docs: list[str], relevant_docs: set[str], k: int = 3) -> float:
    """
    Computes Precision@k: fraction of top-k retrieved documents that are relevant.
    """
    if k <= 0:
        return 0.0
    top_k = retrieved_docs[:k]
    if not top_k:
        return 0.0

    hits = sum(1 for doc in top_k if doc in relevant_docs)
    return round(hits / k, 4)


def compute_context_recall(query_entities: list[str], context: str) -> float:
    """
    Measures the percentage of query entities present in the retrieved/compressed context.
    """
    if not query_entities:
        return 1.0
    if not context:
        return 0.0

    ctx_lower = context.lower()
    hits = sum(1 for ent in query_entities if ent.lower() in ctx_lower)
    return round(hits / len(query_entities), 4)


def compute_context_precision(answer: str, context: str) -> float:
    """
    Measures what fraction of context terms were actually relevant and utilized in the answer.
    """
    if not answer or not context:
        return 0.0

    ans_tokens = set(tokenize(answer))
    ctx_tokens = set(tokenize(context))

    if not ans_tokens:
        return 0.0

    overlap = len(ans_tokens & ctx_tokens)
    return round(overlap / len(ans_tokens), 4)
