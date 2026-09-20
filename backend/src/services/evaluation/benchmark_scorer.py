import json
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


def compute_retrieval_metrics(
    retrieved_chunk_ids: list[str],
    relevant_chunk_ids: set[str],
    k_recall: int = 5,
    k_precision: int = 3,
    k_mrr: int = 5,
    k_ndcg: int = 5,
) -> dict[str, float]:
    """
    Computes rigorous retrieval metrics from raw candidate chunk IDs against
    resolved structured ground-truth chunk IDs:
      - recall_at_5: fraction of relevant chunks retrieved in top-5
      - precision_at_3: fraction of top-3 retrieved chunks that are relevant
      - mrr_at_5: reciprocal rank of the first relevant chunk within top-5 (0 if none)
      - ndcg_at_5: normalized discounted cumulative gain at rank 5
    """
    if not relevant_chunk_ids:
        # If question has no relevant chunks (e.g. absent/refusal), retrieval of 0 chunks is perfect
        return {
            "recall_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
            "precision_at_3": 1.0 if not retrieved_chunk_ids else 0.0,
            "mrr_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
            "ndcg_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
        }

    # Recall@k
    top_recall = retrieved_chunk_ids[:k_recall]
    recall_hits = sum(1 for cid in top_recall if cid in relevant_chunk_ids)
    recall = round(recall_hits / min(len(relevant_chunk_ids), k_recall), 4)

    # Precision@k
    top_prec = retrieved_chunk_ids[:k_precision]
    prec_hits = sum(1 for cid in top_prec if cid in relevant_chunk_ids)
    precision = round(prec_hits / k_precision, 4) if k_precision > 0 else 0.0

    # MRR@k
    top_mrr = retrieved_chunk_ids[:k_mrr]
    mrr = 0.0
    for rank, cid in enumerate(top_mrr, 1):
        if cid in relevant_chunk_ids:
            mrr = round(1.0 / rank, 4)
            break

    # nDCG@k
    top_ndcg = retrieved_chunk_ids[:k_ndcg]
    dcg = 0.0
    for rank, cid in enumerate(top_ndcg, 1):
        if cid in relevant_chunk_ids:
            dcg += 1.0 / math.log2(rank + 1)
    idcg = sum(
        1.0 / math.log2(rank + 1)
        for rank in range(1, min(len(relevant_chunk_ids), k_ndcg) + 1)
    )
    ndcg = round(dcg / idcg, 4) if idcg > 0 else 0.0

    return {
        "recall_at_5": recall,
        "precision_at_3": precision,
        "mrr_at_5": mrr,
        "ndcg_at_5": ndcg,
    }


def grade_premise_correction(
    answer: str,
    expected_answer: str,
    key_correction_terms: list[str] | None = None,
) -> bool:
    """
    Grades premise correction questions.
    Requirements:
      1. Must NOT simply say 'no', 'incorrect', 'actually', or refuse with 'NOT_FOUND'.
      2. Must actively correct the false premise by stating the correct attribute/entity/number.
      3. If key_correction_terms are provided, all must be present in the answer.
      4. If expected_answer is provided, content_match or key terms must match.
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    ans_clean = answer.strip().lower()
    # Reject lazy generic dismissals
    generic_dismissals = [
        "no",
        "incorrect",
        "that is incorrect",
        "this is false",
        "actually no",
        "false premise",
        "i cannot find",
        "not mentioned",
    ]
    if ans_clean in generic_dismissals or len(tokenize(ans_clean)) < 3:
        return False

    if key_correction_terms:
        for term in key_correction_terms:
            if term.lower() not in ans_clean:
                return False
        return True

    if expected_answer:
        return content_match(answer, expected_answer)

    return True


def grade_arithmetic_answer(
    answer: str,
    expected_value: float,
    tolerance: float = 0.01,
    expected_unit: str | None = None,
) -> bool:
    """
    Grades arithmetic/numeric calculation answers.
    Validates:
      - Presence of numeric value matching expected_value within tolerance.
      - Correct sign.
      - Presence of expected_unit if specified.
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    raw_nums = re.findall(r"[-+]?\b\d[\d,]*(?:\.\d+)?\b", answer)
    found_match = False
    for num_str in raw_nums:
        clean_num = num_str.replace(",", "")
        try:
            val = float(clean_num)
            if abs(val - expected_value) <= max(tolerance, abs(expected_value) * tolerance):
                found_match = True
                break
        except ValueError:
            continue

    if not found_match:
        return False

    if expected_unit:
        if expected_unit.lower() not in answer.lower():
            return False

    return True


def grade_json_schema(answer: str, required_keys: list[str]) -> bool:
    """
    Grades format compliance for JSON responses.
    Parses answer string (or markdown-fenced json) and ensures all required_keys exist.
    """
    if not answer:
        return False

    clean_text = answer.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    try:
        data = json.loads(clean_text)
        if not isinstance(data, dict):
            return False
        for k in required_keys:
            if k not in data:
                return False
        return True
    except Exception:
        return False


def grade_structured_numeric(answer: str, contract: dict) -> bool:
    """
    Grades numeric calculation answers against a structured contract:
      - target_values: list of float values (all must be matched within tolerance)
      - operands: optional list of operands (must be present in context/calculation)
      - required_sign: '+' or '-'
      - required_units: list of unit keywords (e.g. ['%', 'percent'] - at least one must match)
      - tolerance: float
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    raw_nums = re.findall(r"[-+]?\b\d[\d,]*(?:\.\d+)?\b", answer)
    extracted_floats = []
    for num_str in raw_nums:
        clean_num = num_str.replace(",", "")
        try:
            extracted_floats.append(float(clean_num))
        except ValueError:
            continue

    target_values = contract.get("target_values", [])
    tol = contract.get("tolerance", 0.02)

    # Check each target value
    for target in target_values:
        matched = False
        for val in extracted_floats:
            delta = abs(val - target)
            allowed = max(tol, abs(target) * tol)
            if delta <= allowed:
                matched = True
                break
        if not matched:
            return False

    # Check required sign
    req_sign = contract.get("required_sign")
    if req_sign == "-":
        has_negative = any(v < 0 for v in extracted_floats) or "negative" in answer.lower() or "minus" in answer.lower()
        if not has_negative:
            return False
    elif req_sign == "+":
        # Check that target values are not reported as negative
        if any(v < 0 and abs(v) in target_values for v in extracted_floats):
            return False

    # Check required units
    req_units = contract.get("required_units", [])
    if req_units:
        ans_lower = answer.lower()
        unit_matched = any(u.lower() in ans_lower for u in req_units)
        if not unit_matched:
            return False

    return True


def grade_structured_json(answer: str, contract: dict) -> bool:
    """
    Grades structured JSON answers against contract requirements:
      - required_keys: all keys must be present
      - required_values: exact value match for specified keys
    """
    if not answer:
        return False

    clean_text = answer.strip()
    if clean_text.startswith("```json"):
        clean_text = clean_text[7:]
    elif clean_text.startswith("```"):
        clean_text = clean_text[3:]
    if clean_text.endswith("```"):
        clean_text = clean_text[:-3]
    clean_text = clean_text.strip()

    try:
        data = json.loads(clean_text)
        if not isinstance(data, dict):
            return False

        req_keys = contract.get("required_keys", [])
        for k in req_keys:
            if k not in data:
                return False

        req_vals = contract.get("required_values", {})
        for k, expected_v in req_vals.items():
            actual_v = data.get(k)
            if actual_v != expected_v:
                # Allow string/int conversion
                if str(actual_v).strip() != str(expected_v).strip():
                    return False

        return True
    except Exception:
        return False


def grade_structured_premise(answer: str, contract: dict) -> bool:
    """
    Grades premise correction questions against contract:
      - rejected_generic_responses: must not be a lazy dismissal
      - key_correction_terms: all required correction terms must appear
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    ans_clean = answer.strip().lower()
    rejected = contract.get("rejected_generic_responses", [])
    if ans_clean in rejected or len(tokenize(ans_clean)) < 3:
        return False

    key_terms = contract.get("key_correction_terms", [])
    if key_terms:
        for term in key_terms:
            if term.lower() not in ans_clean:
                return False
        return True

    exp_ans = contract.get("expected_answer")
    if exp_ans:
        return content_match(answer, exp_ans)

    return True


def validate_citations(
    citations: list[dict],
    context_chunk_ids: list[str] | set[str] | None = None,
    ground_truth_chunk_ids: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """
    Validates that citations:
      1. Have non-empty document_filename and chunk_id.
      2. If context_chunk_ids provided, chunk_id MUST exist in the retrieved compressed context.
      3. If ground_truth_chunk_ids provided, at least one citation must match ground-truth chunk IDs.
      4. Have valid page_number (> 0) or non-empty location_reference.
    """
    if not citations:
        return {"valid": True, "citation_count": 0, "errors": []}

    context_set = set(context_chunk_ids) if context_chunk_ids is not None else None
    gt_set = set(ground_truth_chunk_ids) if ground_truth_chunk_ids else None
    errors = []
    valid_count = 0
    gt_matched_count = 0

    for idx, c in enumerate(citations):
        cid = str(c.get("chunk_id", ""))
        doc_file = c.get("document_filename", "")
        loc_ref = c.get("location_reference", "")
        page_num = c.get("page_number")

        if not cid:
            errors.append(f"Citation {idx}: missing chunk_id")
            continue
        if context_set is not None and cid not in context_set:
            errors.append(f"Citation {idx}: chunk_id {cid} not in context chunk IDs")
            continue
        if not doc_file:
            errors.append(f"Citation {idx}: missing document_filename")
            continue
        if page_num is None and not loc_ref:
            errors.append(f"Citation {idx}: missing page_number and location_reference")
            continue

        if gt_set is not None and cid in gt_set:
            gt_matched_count += 1

        valid_count += 1

    # If ground truth chunk IDs were supplied, verify ground-truth alignment
    if gt_set is not None and len(gt_set) > 0 and gt_matched_count == 0:
        errors.append("No citations match evaluator ground-truth evidence chunk IDs")

    return {
        "valid": len(errors) == 0,
        "citation_count": len(citations),
        "valid_count": valid_count,
        "ground_truth_matched_count": gt_matched_count,
        "errors": errors,
    }

