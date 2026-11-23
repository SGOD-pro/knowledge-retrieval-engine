import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from services.retrieval.evidence_contract import EvidenceItem, restore_evidence_item

SCORER_VERSION = "2.3.0"



def tokenize(text: str) -> list[str]:
    """Tokenizes text into lowercase alphanumeric tokens of length > 2."""
    if not text:
        return []
    return [w.lower() for w in re.findall(r"\w+", text) if len(w) > 2]


def is_concise_percentage_candidate(text: str) -> bool:
    """Returns True if candidate answer is a concise response with percentage or numeric tokens."""
    if not text:
        return False
    pct_or_num = re.findall(r"[-+]?[$]?\b\d[\d,]*(?:\.\d+)?%?\b", text)
    words = re.findall(r"\b\w+\b", text)
    return len(words) <= 6 and bool(pct_or_num)


def is_concise_percentage_match(chunk_text: str, expected_answer: str) -> bool:
    """Evaluates whether candidate answer text satisfies a single percentage request concisely.

    Requirements:
      1. Expected answer must contain exactly one percentage entity.
      2. Expected answer must not contain other un-matched numeric claims or named entities
         (rejecting answers that omit other required claims).
      3. Candidate answer must explicitly assert the percentage with proper unit ('%' or 'percent'),
         rejecting bare numbers or wrong units (e.g. '$99.58', '99.58').
      4. Candidate percentage value must match the expected percentage within absolute tolerance 0.02,
         rejecting wrong values.
      5. Candidate percentage value must not be negated in the candidate answer,
         rejecting negated values (e.g. 'not 99.58%', 'is not 99.58%').
    """
    if not chunk_text or not expected_answer:
        return False

    pct_regex = re.compile(r"([-+]?\b\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
    exp_pct_matches = pct_regex.findall(expected_answer)
    if len(exp_pct_matches) != 1:
        return False

    try:
        exp_pct_val = Decimal(exp_pct_matches[0].replace(",", ""))
    except InvalidOperation:
        return False

    # Check if expected answer requires other numbers (omitted claims control)
    cleaned_exp = re.sub(r"(?<=\d),(?=\d)", "", expected_answer)
    all_exp_nums = re.findall(r"[-+]?\b\d+(?:\.\d+)?\b", cleaned_exp)
    non_pct_nums = []
    for n in all_exp_nums:
        try:
            if abs(Decimal(n) - exp_pct_val) > Decimal("0.001"):
                non_pct_nums.append(n)
        except InvalidOperation:
            pass
    if non_pct_nums:
        return False

    # Check if expected answer has named entities / proper nouns
    sentences = re.split(r"[.!?]\s*", expected_answer.strip())
    named_entities = set()
    for s in sentences:
        words = re.findall(r"\b[A-Za-z]+\b", s)
        for w in words[1:]:
            if w[0].isupper() and w.lower() not in (
                "january", "february", "march", "april", "may", "june",
                "july", "august", "september", "october", "november", "december"
            ):
                named_entities.add(w.lower())
    if named_entities:
        chunk_words = set(w.lower() for w in re.findall(r"\b[A-Za-z]+\b", chunk_text))
        if not (named_entities <= chunk_words):
            return False

    # Candidate must assert percentage with valid percentage unit
    chunk_pct_matches = pct_regex.findall(chunk_text)
    if not chunk_pct_matches:
        return False

    # Value check within tolerance
    matched_val = False
    for m in chunk_pct_matches:
        try:
            val = Decimal(m.replace(",", ""))
            if abs(val - exp_pct_val) <= Decimal("0.02"):
                matched_val = True
                break
        except InvalidOperation:
            continue
    if not matched_val:
        return False

    # Negation check
    target_str = exp_pct_matches[0]
    if _is_entity_negated_in_text(target_str, chunk_text):
        return False

    return True


def content_match(chunk_text: str, expected_answer: str) -> bool:
    """Canonical benchmark content match scoring function.

    Returns True if:
      - Asymmetric token recall >= 0.50 (fraction of expected answer tokens present in chunk), OR
      - When numbers or percentages are in the expected answer, >= 50% numeric overlap with >= 25% token recall, OR
      - When a single percentage is requested, candidate answer satisfies concise percentage matching.
    Also ensures reasonable token precision when chunk text is short.
    """
    if not expected_answer or not chunk_text:
        return False

    pct_regex = re.compile(r"([-+]?\b\d[\d,]*(?:\.\d+)?)\s*(?:%|percent\b)", re.IGNORECASE)
    exp_pct_matches = pct_regex.findall(expected_answer)

    # When candidate is a concise percentage or numeric response, evaluate via concise percentage matching
    if is_concise_percentage_candidate(chunk_text) and exp_pct_matches:
        return is_concise_percentage_match(chunk_text, expected_answer)

    # When expected answer requests a single percentage, block fall-through if target percentage is negated in candidate
    if len(exp_pct_matches) == 1:
        target_str = exp_pct_matches[0]
        if _is_entity_negated_in_text(target_str, chunk_text):
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
    if not answer:
        return None

    if not isinstance(answer, str):
        answer = json.dumps(answer) if isinstance(answer, (dict, list)) else str(answer)

    if answer in ("NOT_FOUND", "", "The context does not provide"):
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

    Duplicate chunk IDs in retrieved_chunk_ids are deduplicated before scoring;
    duplicates inflate DCG and recall beyond 1.0 and are a measurement defect.
    """
    # Deduplicate retrieved IDs while preserving rank order
    seen: set[str] = set()
    deduped: list[str] = []
    for cid in retrieved_chunk_ids:
        if cid not in seen:
            seen.add(cid)
            deduped.append(cid)
    retrieved_chunk_ids = deduped

    if not relevant_chunk_ids:
        # If question has no relevant chunks (e.g. absent/refusal), retrieval of 0 chunks is perfect
        return {
            "recall_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
            "precision_at_3": 1.0 if not retrieved_chunk_ids else 0.0,
            "mrr_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
            "ndcg_at_5": 1.0 if not retrieved_chunk_ids else 0.0,
        }

    # Recall@k: fraction of relevant chunks hit in top-k
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


def _split_into_clauses(text: str) -> list[str]:
    """Split text into semantic clauses on semicolons, commas, and key conjunctions.
    Comma-split is avoided when the comma is between two digits (e.g. '4,191').
    """
    # Split on semicolons or conjunctions; leave comma-separated numbers intact
    parts = re.split(r';|\band\b|\bbut\b|\bwhere\b|,(?!\s*\d)', text, flags=re.IGNORECASE)
    return [p.strip() for p in parts if p.strip()]


# Role keyword synonyms: any of these words in a clause can satisfy the named role.
_ROLE_SYNONYMS: dict[str, list[str]] = {
    "dimension": ["dimension", "dimensional", "size", "dim", "dimensionality", "length"],
    "annotation": ["annotation", "annotated", "annotations"],
    "count":      ["count", "number", "vectors", "items", "elements", "outputs"],
}


def _role_keyword_in_clause(role_name: str, clause_lower: str) -> bool:
    """Return True if the role or any of its synonyms appear in the clause."""
    synonyms = _ROLE_SYNONYMS.get(role_name.lower(), [role_name.lower()])
    return any(syn in clause_lower for syn in synonyms)


def _numeric_adjacent_to_role(clause: str, role_name: str, target: "Decimal", tol: "Decimal") -> bool:
    """True if `target` is the number most immediately before or modifying role_name in `clause`.

    Strategy:
    1. Tokenise the clause into alternating number/word spans.
    2. Find the position of role_name (or synonym) within the token list.
    3. The nearest number *to the left* of the role keyword wins the binding.
       If two numbers are equidistant, reject (ambiguous).
    4. If the role keyword appears *after* a number and before the next number,
       the number immediately before the role wins.

    This handles:
    - '196 annotation vectors' → 196 binds annotation  ✓
    - '512 annotation vectors; 196-dimensional' → 512 binds annotation (wrong) ✗
    - '196 annotation feature vectors of size 512' → 196 binds annotation ✓, 512 binds dimension ✓
    """
    from decimal import Decimal, InvalidOperation

    # Normalise Unicode minus before parsing
    clause_norm = clause.replace("\u2212", "-")

    synonyms = _ROLE_SYNONYMS.get(role_name.lower(), [role_name.lower()])

    # Build token list: each token is (kind, value, start_char)
    # kind = 'num' | 'role' | 'word'
    token_re = re.compile(
        r"(?P<num>[-+]?\b\d[\d,]*(?:\.\d+)?\b)"
        r"|(?P<word>[A-Za-z][A-Za-z0-9\-]*)",
        re.IGNORECASE,
    )
    tokens = []
    for m in token_re.finditer(clause_norm):
        if m.group("num"):
            try:
                tokens.append(("num", Decimal(m.group("num").replace(",", "")), m.start()))
            except InvalidOperation:
                pass
        elif m.group("word"):
            word = m.group("word").lower()
            kind = "role" if any(syn == word for syn in synonyms) else "word"
            tokens.append((kind, word, m.start()))

    # Find role positions
    role_positions = [i for i, t in enumerate(tokens) if t[0] == "role"]
    if not role_positions:
        return False

    for rpos in role_positions:
        # Find the nearest number to the LEFT of this role occurrence
        nums_left = [(i, tokens[i][1]) for i in range(rpos) if tokens[i][0] == "num"]
        if nums_left:
            nearest_left_idx, nearest_left_val = nums_left[-1]
            if abs(nearest_left_val - target) <= tol:
                return True
        else:
            # No number to the left: check the number immediately to the RIGHT
            # (e.g. 'each of size 512' — 'size' is the role, 512 is to its right)
            nums_right = [(i, tokens[i][1]) for i in range(rpos + 1, len(tokens))
                          if tokens[i][0] == "num"]
            if nums_right:
                nearest_right_idx, nearest_right_val = nums_right[0]
                if abs(nearest_right_val - target) <= tol:
                    return True

    return False


def grade_structured_numeric(answer: str, contract: dict) -> bool:
    """
    Grades numeric calculation answers against a structured contract.

    Contract fields:
      - target_values: list of Decimal/float values; all must be matched within
        strict *absolute* tolerance (not scaled). Use Decimal for precision.
      - required_roles: list of {"value": <number>, "role": <str>} dicts; the
        answer must associate the value with the named role using clause-based
        binding (nearest-left number per clause). Synonyms are resolved for
        well-known roles (dimension, annotation, count).
      - required_sign: '-' means a negative signal must be present (negative number,
        Unicode minus U+2212, or decrease direction word); '+' means positive.
      - required_direction: 'decrease' or 'increase'; for change questions.
      - required_units: list of unit keywords; at least one must appear.
      - tolerance: strict absolute tolerance (default 0.02). Not scaled by target.
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    # Normalise Unicode minus (U+2212) to ASCII hyphen-minus before any processing
    answer_norm = answer.replace("\u2212", "-")
    ans_lower = answer_norm.lower()

    def _parse_decimals(text: str) -> list[Decimal]:
        # Normalise unicode minus first
        text = text.replace("\u2212", "-")
        nums = re.findall(r"[-+]?\b\d[\d,]*(?:\.\d+)?\b", text)
        result = []
        for n in nums:
            try:
                result.append(Decimal(n.replace(",", "")))
            except InvalidOperation:
                pass
        return result

    extracted = _parse_decimals(answer_norm)
    tol = Decimal(str(contract.get("tolerance", 0.02)))
    target_values = [Decimal(str(v)) for v in contract.get("target_values", [])]

    # --- Required direction (increase / decrease) ---
    req_direction = contract.get("required_direction")
    if req_direction == "decrease":
        decrease_words = {"decrease", "decreased", "fell", "fall", "falling", "decline",
                          "declined", "dropped", "reduced", "lower", "loss", "negative",
                          "contraction", "shrunk", "shrinkage"}
        increase_words = {"increase", "increased", "grew", "growth", "rose", "risen", "gain",
                          "positive", "higher", "improvement", "addition"}
        words_in_ans = set(re.findall(r"\b\w+\b", ans_lower))
        has_decrease = bool(words_in_ans & decrease_words)
        has_increase = bool(words_in_ans & increase_words)
        # Reject if affirms increase without also asserting decrease
        if has_increase and not has_decrease:
            return False
        if not has_decrease:
            return False
    elif req_direction == "increase":
        increase_words = {"increase", "grew", "growth", "rose", "gain", "positive change",
                          "higher", "improvement"}
        words_in_ans = set(re.findall(r"\b\w+\b", ans_lower))
        if not (words_in_ans & increase_words):
            return False

    # --- Required roles: clause-based value-role binding ---
    required_roles = contract.get("required_roles", [])
    clauses = _split_into_clauses(answer_norm)
    for role_spec in required_roles:
        rv = Decimal(str(role_spec["value"]))
        role_name = role_spec["role"].lower()

        # Search every clause that contains the role keyword (or synonym)
        binding_found = False
        for clause in clauses:
            clause_lower = clause.lower()
            if _role_keyword_in_clause(role_name, clause_lower):
                if _numeric_adjacent_to_role(clause, role_name, rv, tol):
                    binding_found = True
                    break
        if not binding_found:
            return False

    # --- Check each target value using strict absolute tolerance ---
    # When required_sign is '-' or 'negative', extracted values may be negative (e.g. -4191 from
    # '-4,191 million'). A positive target (4191.0) matches its negated form abs(ev) as
    # well as the literal form, so signed answers satisfy the magnitude requirement.
    req_sign_for_matching = contract.get("required_sign")
    for target in target_values:
        if req_sign_for_matching in ("-", "negative") and target > 0:
            matched = any(abs(ev - target) <= tol or abs(abs(ev) - target) <= tol for ev in extracted)
        else:
            matched = any(abs(ev - target) <= tol for ev in extracted)
        if not matched:
            return False

    # --- Required sign ---
    req_sign = contract.get("required_sign")
    if req_sign in ("-", "negative"):
        has_negative = (
            any(v < 0 for v in extracted)
            or "negative" in ans_lower
            or "minus" in ans_lower
            or "decrease" in ans_lower
            or "decreased" in ans_lower
            or "fell" in ans_lower
            or "fall" in ans_lower
            or "decline" in ans_lower
            or "declined" in ans_lower
            or "dropped" in ans_lower
            or "reduced" in ans_lower
        )
        if not has_negative:
            return False
    elif req_sign in ("+", "positive"):
        if any(v < 0 and any(abs(v + t) <= tol for t in target_values) for v in extracted):
            return False

    # --- Required units ---
    req_units = contract.get("required_units", [])
    if req_units:
        unit_matched = any(u.lower() in ans_lower for u in req_units)
        if not unit_matched:
            return False

    return True


def grade_structured_json(answer: str, contract: dict) -> bool:
    """
    Grades structured JSON answers against contract requirements:
      - required_keys: all keys must be present
      - required_values: exact value match including type (int stays int, etc.)
      - allow_extra_keys: if False (default), extra keys cause failure
      - strict_format: if True, Markdown fences cause failure
    """
    if not answer:
        return False

    clean_text = answer.strip()
    strict_format = contract.get("strict_format", False)

    # Detect Markdown fences
    has_fence = clean_text.startswith("```")
    if strict_format and has_fence:
        return False

    # Strip fences for parsing (when strict_format is False)
    if not strict_format:
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

        # By default, reject extra keys (strict schema)
        allow_extra = contract.get("allow_extra_keys", False)
        if not allow_extra:
            if set(data.keys()) != set(req_keys):
                return False

        req_vals = contract.get("required_values", {})
        for k, expected_v in req_vals.items():
            actual_v = data.get(k)
            # Strict type AND value equality: int 108 ≠ float 108.0; bool True ≠ int 1
            if type(actual_v) is not type(expected_v) or actual_v != expected_v:
                return False

        return True
    except Exception:
        return False


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using a decimal-aware boundary.
    Does NOT split on '.' that is immediately preceded and followed by digits
    (e.g. '$2.03' stays intact).
    """
    # We split using re.split with a decimal-aware sentence-boundary pattern.
    # Sentence boundary: a period NOT flanked by digits on both sides,
    # followed by optional whitespace and an uppercase letter OR end of string.
    # Also split on '!' and '?' followed by whitespace.
    parts = re.split(
        r'(?<!\d)\.(?!\d)(?=\s+[A-Z]|\s*$)'
        r'|[!?](?=\s+[A-Z]|\s*$)',
        text,
    )
    return [p.strip() for p in parts if p.strip()]


def _sentence_is_locally_negated(sentence: str) -> bool:
    """True only if THIS sentence leads with a negation marker (first 5 tokens).
    A negation in a previous sentence does NOT propagate here.
    """
    tokens = re.findall(r'\b\w+\b', sentence.lower())[:5]
    negation_starters = {"no", "not", "incorrect", "wrong", "false", "never",
                         "isn't", "doesn't", "cannot", "can't"}
    return bool(set(tokens) & negation_starters)


def grade_structured_premise(answer: str, contract: dict) -> bool:
    """
    Grades premise correction questions against contract:
      - rejected_generic_responses: must not be a lazy exact-match dismissal
      - reject_if_affirms_any: list of phrases; each sentence is checked independently.
        A sentence fails if it contains a reject phrase AND is not locally negated
        (leading negation marker within first 5 tokens of THAT sentence).
        A negation in a prior sentence does NOT pardon a false assertion in a
        subsequent sentence.
      - key_correction_terms: all required correction terms must appear in the answer
      - expected_answer: fallback content_match check when no key_correction_terms

    An answer that affirms the false premise in any sentence (without local negation)
    fails even if other sentences contain correction language.
    """
    if not answer or answer.strip() in ("NOT_FOUND", ""):
        return False

    ans_clean = answer.strip().lower()

    # Reject lazy exact-match dismissals
    rejected = contract.get("rejected_generic_responses", [])
    if ans_clean in rejected or len(tokenize(ans_clean)) < 3:
        return False

    # Reject if any sentence affirms the false premise without sentence-local negation
    reject_affirmations = contract.get("reject_if_affirms_any", [])
    if reject_affirmations:
        sentences = _split_sentences(answer.strip())
        for sentence in sentences:
            sentence_lower = sentence.lower()
            for phrase in reject_affirmations:
                if phrase.lower() in sentence_lower:
                    # Only pardon if THIS sentence starts with a negation
                    if not _sentence_is_locally_negated(sentence):
                        return False
                    # Even a locally-negated sentence fails if it contains a reject
                    # phrase that is NOT itself negated — e.g. "No, the correct unit
                    # is wrong" → sentence starts with "No" but "correct unit" is
                    # an affirmation phrase. We only pardon when the reject phrase
                    # is part of the corrective statement (i.e. used to deny the premise).
                    # Since we cannot reliably detect this without NLI, we apply a
                    # conservative rule: a locally-negated sentence that contains a
                    # reject phrase passes ONLY if the reject phrase is immediately
                    # preceded by a negation word within 6 tokens.
                    phrase_pos = sentence_lower.find(phrase.lower())
                    preceding = sentence_lower[max(0, phrase_pos - 50):phrase_pos]
                    preceding_tokens = re.findall(r'\b\w+\b', preceding)[-6:]
                    local_negators = {"no", "not", "incorrect", "wrong", "false",
                                      "never", "isn't", "doesn't", "cannot"}
                    if not set(preceding_tokens) & local_negators:
                        return False

    # All key correction terms must appear verbatim
    key_terms = contract.get("key_correction_terms", [])
    if key_terms:
        for term in key_terms:
            if term.lower() not in ans_clean:
                return False
        return True

    # Fallback: content_match against expected_answer
    exp_ans = contract.get("expected_answer")
    if exp_ans:
        return content_match(answer, exp_ans)

    return True


def extract_numbers_normalized(text: str) -> set[str]:
    """Extracts normalized numbers (including percentages, floats, ints, removing commas) from text."""
    if not text:
        return set()
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
    raw_nums = re.findall(r"\b\d+(?:\.\d+)?%?\b", cleaned)
    nums = set()
    for n in raw_nums:
        nums.add(n)
        clean = n.rstrip("%")
        nums.add(clean)
        try:
            f = float(clean)
            if f.is_integer():
                nums.add(str(int(f)))
            else:
                nums.add(str(f))
        except ValueError:
            pass
    if re.search(r"\b(?:zero|none)\b", text, re.IGNORECASE):
        nums.add("0")
    return nums


def _get_ev_text(ev: Any) -> str:
    """Extract authoritative text from an EvidenceItem, namespace, or dict."""
    if not ev:
        return ""
    if isinstance(ev, dict):
        return str(ev.get("text") or ev.get("content") or (ev.get("locator") or {}).get("text") or "")
    txt = getattr(ev, "text", None) or getattr(ev, "content", None)
    if txt:
        return str(txt)
    loc = getattr(ev, "locator", {}) or {}
    if isinstance(loc, dict) and loc.get("text"):
        return str(loc.get("text"))
    sp = getattr(ev, "structured_payload", {}) or {}
    if isinstance(sp, dict) and sp.get("text"):
        return str(sp.get("text"))
    cp = getattr(ev, "citation_payload", {}) or {}
    if isinstance(cp, dict) and cp.get("text"):
        return str(cp.get("text"))
    return ""


def _ev_matches_citation(ev: Any, c: dict[str, Any]) -> bool:
    """Match an evidence item against a citation dictionary."""
    if not ev or not c:
        return False
    cid = str(c.get("chunk_id", ""))
    sel_hash = str(c.get("selection_hash", ""))
    if isinstance(ev, dict):
        ev_id = str(ev.get("evidence_id", ""))
        loc = ev.get("locator") or {}
        cp = ev.get("citation_payload") or {}
        sp = ev.get("structured_payload") or {}
    else:
        ev_id = str(getattr(ev, "evidence_id", ""))
        loc = getattr(ev, "locator", {}) or {}
        cp = getattr(ev, "citation_payload", {}) or {}
        sp = getattr(ev, "structured_payload", {}) or {}

    ev_cid = str(loc.get("chunk_id", "")) or str(cp.get("chunk_id", ""))
    ev_shash = str(sp.get("selection_hash", "")) or str(loc.get("selection_hash", "")) or str(cp.get("selection_hash", ""))

    if cid and cid in (ev_id, ev_cid):
        return True
    if sel_hash and sel_hash == ev_shash:
        return True
    return False


def validate_citations(
    citations: list[dict],
    context_chunk_ids: list[str] | set[str] | None = None,
    ground_truth_chunk_ids: list[str] | set[str] | None = None,
    retained_structured_hashes: set[str] | None = None,
    retained_evidence_items: list[Any] | None = None,
    expected_sources: list[str] | set[str] | None = None,
    expected_source_version: int | str | None = None,
) -> dict[str, Any]:
    """Validates that citations:
      1. Have non-empty document_filename and chunk_id.
      2. If context_chunk_ids provided, chunk_id MUST exist in the retrieved compressed context.
      3. If ground_truth_chunk_ids provided, at least one citation must match ground-truth chunk IDs.
      4. Have valid page_number (> 0) or non-empty location_reference.
      5. Structured-aggregate citations: selection_hash must be 16 hex chars AND, when
         retained_structured_hashes or retained_evidence_items is provided, must match an actual query
         executed and retained for this request (not invented).
      6. When retained_evidence_items is provided, every citation must map back to an EvidenceItem
         retained for this request.
      7. If expected_sources provided, every citation's document/table source must match expected sources.
      8. If expected_source_version provided, every citation's document_version must match expected version.
    """
    if not citations:
        return {
            "valid": True,
            "citation_count": 0,
            "valid_count": 0,
            "ground_truth_matched_count": 0,
            "errors": [],
        }

    # Extract retained hashes and chunk IDs from retained_evidence_items if provided
    retained_chunk_ids: set[str] | None = None
    all_retained_hashes: set[str] | None = set(retained_structured_hashes) if retained_structured_hashes is not None else None

    if retained_evidence_items is not None:
        retained_evidence_items = [restore_evidence_item(it) for it in retained_evidence_items]
        retained_chunk_ids = set()
        if all_retained_hashes is None:
            all_retained_hashes = set()
        for item in retained_evidence_items:
            ev_id = str(getattr(item, "evidence_id", ""))
            if ev_id:
                retained_chunk_ids.add(ev_id)
            locator = getattr(item, "locator", {}) or {}
            c_locator_id = locator.get("chunk_id")
            if c_locator_id:
                retained_chunk_ids.add(str(c_locator_id))
            c_payload = getattr(item, "citation_payload", {}) or {}
            cp_id = c_payload.get("chunk_id")
            if cp_id:
                retained_chunk_ids.add(str(cp_id))

            # Structured payload / hashes
            sp = getattr(item, "structured_payload", {}) or {}
            shash = sp.get("selection_hash") or locator.get("selection_hash") or c_payload.get("selection_hash")
            if shash:
                all_retained_hashes.add(str(shash))

    context_set = set(context_chunk_ids) if context_chunk_ids is not None else None
    gt_set = set(ground_truth_chunk_ids) if ground_truth_chunk_ids else None
    exp_sources_set = {str(s) for s in expected_sources} if expected_sources else None
    errors = []
    valid_count = 0
    gt_matched_count = 0

    for idx, c in enumerate(citations):
        if c.get("evidence_type") == "structured_aggregate":
            table_id = c.get("table_id")
            doc_ver = c.get("document_version")
            op = c.get("operator")
            target_col = c.get("target_column")
            sel_hash = c.get("selection_hash")
            sel_count = c.get("selection_count", 0)

            if not table_id:
                errors.append(f"Citation {idx}: missing table_id in structured evidence")
                continue
            if not doc_ver:
                errors.append(f"Citation {idx}: missing document_version in structured evidence")
                continue
            if not op:
                errors.append(f"Citation {idx}: missing operator in structured evidence")
                continue
            if not target_col:
                errors.append(f"Citation {idx}: missing target_column in structured evidence")
                continue
            if not sel_hash or len(sel_hash) != 16:
                errors.append(f"Citation {idx}: invalid selection_hash in structured evidence")
                continue
            if sel_count < 1:
                errors.append(f"Citation {idx}: selection_count must be >= 1 in structured evidence")
                continue

            # Verify source matches expected_sources if provided
            if exp_sources_set:
                c_src = c.get("document_filename") or c.get("source_id") or table_id
                if c_src and c_src not in exp_sources_set and table_id not in exp_sources_set:
                    errors.append(f"Citation {idx}: source '{c_src}' not in expected sources {exp_sources_set}")
                    continue

            # Verify version matches expected_source_version if provided
            if expected_source_version is not None and doc_ver is not None:
                try:
                    if int(doc_ver) != int(expected_source_version):
                        errors.append(f"Citation {idx}: document_version '{doc_ver}' does not match expected version '{expected_source_version}'")
                        continue
                except (ValueError, TypeError):
                    if str(doc_ver) != str(expected_source_version):
                        errors.append(f"Citation {idx}: document_version '{doc_ver}' does not match expected version '{expected_source_version}'")
                        continue

            # Verify hash matches a real executed query when caller supplies retained hashes/evidence
            if all_retained_hashes is not None and sel_hash not in all_retained_hashes:
                errors.append(
                    f"Citation {idx}: selection_hash '{sel_hash}' not in retained execution evidence "
                    f"(fabricated or stale citation)"
                )
                continue

            # A matching hash alone must not validate altered citation fields.
            # When retained_evidence_items is supplied, verify every field matches the retained record.
            if retained_evidence_items is not None:
                matching_ev = None
                for ev_it in retained_evidence_items:
                    sp = getattr(ev_it, "structured_payload", {}) or {}
                    loc = getattr(ev_it, "locator", {}) or {}
                    h = sp.get("selection_hash") or loc.get("selection_hash")
                    if str(h) == str(sel_hash):
                        matching_ev = ev_it
                        break

                if matching_ev is not None:
                    sp = getattr(matching_ev, "structured_payload", {}) or {}
                    loc = getattr(matching_ev, "locator", {}) or {}
                    expected_table = sp.get("table_id") or loc.get("table_id")
                    expected_ver = getattr(matching_ev, "document_version", None) or sp.get("document_version")
                    expected_op = sp.get("operator")
                    expected_col = sp.get("target_column")
                    expected_count = sp.get("selection_count") or loc.get("selection_count")
                    expected_ws = getattr(matching_ev, "workspace_id", None)
                    expected_res = sp.get("result_value")

                    if expected_table and table_id != expected_table:
                        errors.append(f"Citation {idx}: table_id '{table_id}' altered from retained evidence '{expected_table}'")
                        continue
                    if expected_ver and doc_ver != expected_ver:
                        errors.append(f"Citation {idx}: document_version '{doc_ver}' altered from retained evidence '{expected_ver}'")
                        continue
                    if expected_op and op != expected_op:
                        errors.append(f"Citation {idx}: operator '{op}' altered from retained evidence '{expected_op}'")
                        continue
                    if expected_col and target_col != expected_col:
                        errors.append(f"Citation {idx}: target_column '{target_col}' altered from retained evidence '{expected_col}'")
                        continue
                    if expected_count is not None and int(sel_count) != int(expected_count):
                        errors.append(f"Citation {idx}: selection_count {sel_count} altered from retained evidence {expected_count}")
                        continue
                    if expected_ws and c.get("workspace_id") and c.get("workspace_id") != expected_ws:
                        errors.append(f"Citation {idx}: workspace_id '{c.get('workspace_id')}' altered from retained evidence '{expected_ws}'")
                        continue
                    expected_sec = sp.get("secondary_value")

                    # Result value validation
                    if expected_res is not None:
                        if c.get("result_value") is not None and str(c.get("result_value")) != str(expected_res):
                            errors.append(f"Citation {idx}: result_value '{c.get('result_value')}' altered from retained evidence '{expected_res}'")
                            continue
                    else:
                        if c.get("result_value") is not None:
                            errors.append(f"Citation {idx}: result_value '{c.get('result_value')}' fabricated (not in retained evidence)")
                            continue

                    # Secondary value validation
                    if expected_sec is not None:
                        if c.get("secondary_value") is not None and str(c.get("secondary_value")) != str(expected_sec):
                            errors.append(f"Citation {idx}: secondary_value '{c.get('secondary_value')}' altered from retained evidence '{expected_sec}'")
                            continue
                    else:
                        if c.get("secondary_value") is not None:
                            errors.append(f"Citation {idx}: secondary_value '{c.get('secondary_value')}' fabricated (not in retained evidence)")
                            continue

                    # Nested structured_payload validation
                    c_sp = c.get("structured_payload")
                    if isinstance(c_sp, dict):
                        c_sp_res = c_sp.get("result_value")
                        if expected_res is not None:
                            if c_sp_res is not None and str(c_sp_res) != str(expected_res):
                                errors.append(f"Citation {idx}: nested structured_payload result_value '{c_sp_res}' altered from retained evidence '{expected_res}'")
                                continue
                        else:
                            if c_sp_res is not None:
                                errors.append(f"Citation {idx}: nested structured_payload result_value '{c_sp_res}' fabricated (not in retained evidence)")
                                continue

                        c_sp_sec = c_sp.get("secondary_value")
                        if expected_sec is not None:
                            if c_sp_sec is not None and str(c_sp_sec) != str(expected_sec):
                                errors.append(f"Citation {idx}: nested structured_payload secondary_value '{c_sp_sec}' altered from retained evidence '{expected_sec}'")
                                continue
                        else:
                            if c_sp_sec is not None:
                                errors.append(f"Citation {idx}: nested structured_payload secondary_value '{c_sp_sec}' fabricated (not in retained evidence)")
                                continue

                    # Predicates validation
                    cp = getattr(matching_ev, "citation_payload", {}) or {}
                    expected_pcols = cp.get("predicate_columns")
                    expected_pvals = cp.get("predicate_values")
                    if expected_pcols is not None and c.get("predicate_columns") is not None and c.get("predicate_columns") != expected_pcols:
                        errors.append(f"Citation {idx}: predicate_columns altered from retained evidence")
                        continue
                    if expected_pvals is not None and c.get("predicate_values") is not None and c.get("predicate_values") != expected_pvals:
                        errors.append(f"Citation {idx}: predicate_values altered from retained evidence")
                        continue
                    if c.get("predicates") is not None and c.get("predicates") != cp.get("predicates"):
                        errors.append(f"Citation {idx}: predicates altered from retained evidence")
                        continue

                    # Provenance validation
                    expected_prov = getattr(matching_ev, "provenance", None)
                    if expected_prov is not None and c.get("provenance") is not None and c.get("provenance") != expected_prov:
                        errors.append(f"Citation {idx}: provenance altered from retained evidence")
                        continue

            valid_count += 1
            if gt_set is not None:
                gt_matched_count += 1
            continue

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
        if retained_chunk_ids is not None and cid not in retained_chunk_ids:
            errors.append(f"Citation {idx}: chunk_id {cid} not in retained evidence items")
            continue
        if not doc_file:
            errors.append(f"Citation {idx}: missing document_filename")
            continue
        if page_num is None and not loc_ref:
            errors.append(f"Citation {idx}: missing page_number and location_reference")
            continue

        # Verify source matches expected_sources if provided
        if exp_sources_set and doc_file not in exp_sources_set:
            errors.append(f"Citation {idx}: source document '{doc_file}' not in expected sources {exp_sources_set}")
            continue

        # Verify version matches expected_source_version if provided
        c_ver = c.get("document_version") or c.get("source_version")
        if expected_source_version is not None and c_ver is not None:
            try:
                if int(c_ver) != int(expected_source_version):
                    errors.append(f"Citation {idx}: document_version '{c_ver}' does not match expected version '{expected_source_version}'")
                    continue
            except (ValueError, TypeError):
                if str(c_ver) != str(expected_source_version):
                    errors.append(f"Citation {idx}: document_version '{c_ver}' does not match expected version '{expected_source_version}'")
                    continue

        # When retained_evidence_items is supplied, verify regular citation fields match the retained record
        if retained_evidence_items is not None:
            matching_ev = None
            for ev_it in retained_evidence_items:
                ev_id = str(getattr(ev_it, "evidence_id", ""))
                loc = getattr(ev_it, "locator", {}) or {}
                cp = getattr(ev_it, "citation_payload", {}) or {}
                if cid in (ev_id, str(loc.get("chunk_id", "")), str(cp.get("chunk_id", ""))):
                    matching_ev = ev_it
                    break

            if matching_ev is not None:
                exp_doc_file = getattr(matching_ev, "document_filename", None) or (getattr(matching_ev, "locator", {}) or {}).get("document_filename")
                exp_ver = getattr(matching_ev, "document_version", None) or (getattr(matching_ev, "locator", {}) or {}).get("document_version")
                exp_did = getattr(matching_ev, "document_id", None) or (getattr(matching_ev, "locator", {}) or {}).get("document_id")

                if exp_doc_file and doc_file != exp_doc_file:
                    errors.append(f"Citation {idx}: document_filename '{doc_file}' altered from retained evidence '{exp_doc_file}'")
                    continue
                if exp_ver is not None and c_ver is not None:
                    try:
                        if int(c_ver) != int(exp_ver):
                            errors.append(f"Citation {idx}: document_version '{c_ver}' altered from retained evidence '{exp_ver}'")
                            continue
                    except (ValueError, TypeError):
                        if str(c_ver) != str(exp_ver):
                            errors.append(f"Citation {idx}: document_version '{c_ver}' altered from retained evidence '{exp_ver}'")
                            continue
                c_did = c.get("document_id")
                if exp_did and c_did and str(c_did) != str(exp_did):
                    errors.append(f"Citation {idx}: document_id '{c_did}' altered from retained evidence '{exp_did}'")
                    continue

                # Check for fabricated snippet content against authoritative retained evidence
                auth_text = _get_ev_text(matching_ev)
                c_snip = str(c.get("snippet") or c.get("text") or c.get("text_snippet") or "").strip()
                if c_snip:
                    if not auth_text:
                        errors.append(
                            f"Citation {idx}: snippet fabricates supporting content (no text in retained evidence)"
                        )
                        continue
                    snip_nums = extract_numbers_normalized(c_snip)
                    auth_nums = extract_numbers_normalized(auth_text)
                    fabricated_nums = snip_nums - auth_nums
                    if fabricated_nums:
                        errors.append(
                            f"Citation {idx}: snippet fabricates supporting content not in retained evidence "
                            f"(unsupported numbers {fabricated_nums})"
                        )
                        continue
                    snip_faith = compute_faithfulness(c_snip, auth_text)
                    if snip_faith is not None and snip_faith < 0.40:
                        errors.append(
                            f"Citation {idx}: snippet fabricates supporting content not in retained evidence "
                            f"(faithfulness {snip_faith:.2f})"
                        )
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


_NEGATION_WORDS = {
    "not", "never", "no", "neither", "nor", "none",
    "cannot", "cant", "can't", "wont", "won't",
    "incorrect", "wrong", "false", "untrue", "inaccurate",
    "denies", "denied", "denying", "rejects", "rejected", "rejecting",
    "refutes", "refuted", "refuting", "disputes", "disputed", "disputing",
}

_COPULAR_AND_ADVERB_TOKENS = {
    "is", "was", "are", "were", "seems", "seemed", "appears", "appeared",
    "be", "been", "being", "definitely", "certainly", "clearly", "really",
    "actually", "surely", "reported", "stated",
}

_CONJUNCTION_AND_BOUNDARY_TOKENS = {
    "and", "but", "or", "however", "instead", "rather", "although", "whereas",
    "while", "yet", ",", ";", ":",
}

_CONTRAST_PATTERN = re.compile(
    r"\b(?:but|however|instead\s+of|instead|rather\s+than|rather|although|whereas|while|yet|\band\b)\b",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_PATTERN = re.compile(r"(?:(?<!\d)\.|\.(?!\d)|[!?;:\n|]|(?<!\d),|,(?!\d))+")


def _normalize_text_for_negation(text: str) -> str:
    """Strip thousands separators so numbers like 157,000 remain atomic."""
    return re.sub(r"(?<=\d),(?=\d)", "", text)


def _split_into_contradiction_clauses(text: str) -> list[str]:
    """Split text into contradiction clauses by contrast conjunctions and non-numeric punctuation."""
    text = _normalize_text_for_negation(text)
    t = _CONTRAST_PATTERN.sub(" | ", text)
    parts = _CLAUSE_SPLIT_PATTERN.split(t)
    return [p.strip() for p in parts if p.strip()]


def _tokenize(text: str) -> list[str]:
    """Tokenize text into words, alphanumeric identifiers, and numbers."""
    return re.findall(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?:\.[0-9]+)?%?\b", text)


def _target_in_token(target: str, token: str) -> bool:
    """Check if target matches token by exact string, hyphenated part, or numeric equivalence."""
    t_clean = target.lower().strip("$% ")
    tok_clean = token.lower().strip("$% ")
    if t_clean == tok_clean:
        return True
    parts = tok_clean.split("-")
    if t_clean in parts:
        return True
    try:
        if float(t_clean) == float(tok_clean):
            return True
    except ValueError:
        pass
    for p in parts:
        try:
            if float(t_clean) == float(p):
                return True
        except ValueError:
            pass
    return False


def _is_numeric_or_entity_token(token: str) -> bool:
    """Return True if token contains any digits or numbers."""
    tok_clean = token.lower().strip("$% ")
    return bool(re.search(r"\d", tok_clean))


def _is_target_negated_in_clause(target: str, clause: str) -> bool:
    """Check if a specific target is in the scope of negation within a clause.

    Binds negation to its claim rather than nearby numbers indiscriminately:
    1. Preceding negation: governs target only when at most 4 tokens away with
       no intervening entity or coordinating conjunction.
    2. Following negation: governs target only in copular predicate constructions
       (e.g. 'T is not', 'T is incorrect') where all intervening tokens are copulas or adverbs.
    """
    tokens = _tokenize(clause)
    target_indices = [i for i, tok in enumerate(tokens) if _target_in_token(target, tok)]
    if not target_indices:
        return False

    for idx in target_indices:
        # Preceding negation check
        for j in range(max(0, idx - 4), idx):
            tok = tokens[j].lower()
            if tok in _NEGATION_WORDS:
                intervening = [tokens[k].lower() for k in range(j + 1, idx)]
                has_intervening_number = any(_is_numeric_or_entity_token(t) for t in intervening)
                has_intervening_conjunction = any(t in _CONJUNCTION_AND_BOUNDARY_TOKENS for t in intervening)
                if not has_intervening_number and not has_intervening_conjunction:
                    return True

        # Following negation check (copular predicate only)
        for j in range(idx + 1, min(len(tokens), idx + 4)):
            tok = tokens[j].lower()
            if tok in _NEGATION_WORDS:
                intervening = [tokens[k].lower() for k in range(idx + 1, j)]
                if intervening and all(t in _COPULAR_AND_ADVERB_TOKENS for t in intervening):
                    return True

    return False


def _is_entity_negated_in_text(target: str, text: str) -> bool:
    """Check if target is negated in any contradiction clause of text."""
    clauses = _split_into_contradiction_clauses(text)
    for c in clauses:
        if _is_target_negated_in_clause(target, c):
            return True
    return False


def detect_contradiction_against_evidence(
    answer: str,
    context: str = "",
    computed_results: set[str] | None = None,
    contract: dict[str, Any] | None = None,
) -> str | None:
    """Detects claim-level contradiction between answer and authoritative evidence.

    Returns an error reason if a contradiction is detected, or None if sound.
    """
    if not answer:
        return None

    candidates = set()
    if computed_results:
        candidates.update(str(r).strip() for r in computed_results if str(r).strip())

    if contract and contract.get("target_values"):
        for tv in contract.get("target_values", []):
            candidates.update(extract_numbers_normalized(str(tv)))

    if context:
        ids = re.findall(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b", context)
        candidates.update(ids)
        c_nums = extract_numbers_normalized(context)
        candidates.update(c_nums)

    candidates = {c for c in candidates if c and len(c) > 0}

    for cand in candidates:
        ans_negated = _is_entity_negated_in_text(cand, answer)

        if computed_results and cand in computed_results:
            if ans_negated:
                return f"claim_contradicts_evidence: answer negates computed result '{cand}'"
            continue

        if context:
            ctx_negated = _is_entity_negated_in_text(cand, context)
            if not ctx_negated and ans_negated:
                return f"claim_contradicts_evidence: answer negates evidence-affirmed entity '{cand}'"
            if ctx_negated and not ans_negated:
                ans_tokens = _tokenize(answer)
                if any(_target_in_token(cand, tok) for tok in ans_tokens):
                    return f"claim_contradicts_evidence: answer affirms entity '{cand}' which evidence negates"

    return None


def evaluate_grounded_answer(
    answer: str,
    citations: list[dict[str, Any]],
    is_answer_text_correct: bool,
    contract_type: str = "semantic",
    contract: dict[str, Any] | None = None,
    expected_sources: list[str] | set[str] | None = None,
    expected_source_version: int | str | None = None,
    retained_evidence_items: list[Any] | None = None,
    retained_structured_hashes: set[str] | None = None,
    context_chunk_ids: list[str] | set[str] | None = None,
    ground_truth_chunk_ids: list[str] | set[str] | None = None,
    is_infra_error: bool = False,
    ans_is_refusal: bool = False,
    question_type: str = "retrieval",
    outcome: dict[str, Any] | Any | None = None,
) -> dict[str, Any]:
    """Evaluates grounded correctness of an answer requiring text correctness plus

    validated request-retained evidence support.

    Requirements:
      1. Infrastructure failures must fail grounded scoring.
      2. Incorrect answer text fails grounded scoring.
      3. Refusal contracts:
         - Clean refusal with zero citations passes grounded scoring.
         - Fabricating citations on a refusal fails grounded scoring.
      4. Answerable queries:
         - Missing citations fails grounded scoring.
         - Invalid citations (missing fields, wrong source, wrong version, altered retained fields) fails.
         - Claim-level contradiction against authoritative evidence fails grounded scoring.
         - Matching citation IDs alone must not establish claim support:
           - For structured aggregate: answer must assert the operation-specific result_value
             (and not merely metadata like selection_count or total_rows).
           - For text retrieval: citation context snippets must support the answer
             (compute_faithfulness >= 0.25 and numeric claims in the answer must appear in context snippets
             or valid contract target values).
    """
    if retained_evidence_items is not None:
        retained_evidence_items = [restore_evidence_item(it) for it in retained_evidence_items]

    cit_val = validate_citations(
        citations=citations,
        context_chunk_ids=context_chunk_ids,
        ground_truth_chunk_ids=ground_truth_chunk_ids,
        retained_structured_hashes=retained_structured_hashes,
        retained_evidence_items=retained_evidence_items,
        expected_sources=expected_sources,
        expected_source_version=expected_source_version,
    )

    if is_infra_error:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "infrastructure_error",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    is_refusal_required = contract_type == "refusal" or bool(contract and contract.get("is_refusal_required"))

    # Refusal contract evaluation
    if is_refusal_required:
        if citations and len(citations) > 0:
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": "refusal_contract_with_fabricated_citations",
                "citation_validation": cit_val,
                "faithfulness": None,
            }
        if not is_answer_text_correct or not ans_is_refusal:
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": "refusal_contract_failed_to_refuse",
                "citation_validation": cit_val,
                "faithfulness": None,
            }
        return {
            "is_grounded_correct": True,
            "grounded_correct": True,
            "reason": "valid_unsupported_refusal",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    # Non-refusal, answerable query evaluation
    if not is_answer_text_correct:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "answer_text_incorrect",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    if not answer or str(answer).strip() in ("", "NOT_FOUND") or ans_is_refusal:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "empty_or_refusal_on_answerable_query",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    if not citations or len(citations) == 0:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "missing_citations",
            "citation_validation": {"valid": False, "citation_count": 0, "errors": ["Missing citations for answerable query"]},
            "faithfulness": None,
        }

    if not cit_val["valid"]:
        first_err = cit_val["errors"][0] if cit_val["errors"] else "invalid_citations"
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": f"invalid_citations: {first_err}",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    # For answerable queries, authoritative retained request evidence is mandatory
    if not retained_evidence_items or len(retained_evidence_items) == 0:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "missing_authoritative_evidence",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    # Claim support validation: matching citation IDs alone must not establish claim support
    is_structured_agg = (
        question_type == "structured_aggregate"
        or contract_type == "structured_aggregate"
        or any(c.get("evidence_type") == "structured_aggregate" for c in citations)
        or (isinstance(outcome, dict) and outcome.get("structured_aggregate") is True)
        or (isinstance(outcome, dict) and outcome.get("execution_type") == "deterministic")
    )

    if is_structured_agg:
        operators = set()
        explicit_results = set()
        metadata_counts = set()

        def _inspect_d(d: dict):
            op = d.get("operator") or d.get("operation")
            if op:
                operators.add(str(op).lower())
            rv = d.get("result_value")
            if rv is not None and str(rv).strip() != "":
                explicit_results.update(extract_numbers_normalized(str(rv)))
            sv = d.get("secondary_value")
            if sv is not None and str(sv).strip() != "":
                explicit_results.update(extract_numbers_normalized(str(sv)))
            sc = d.get("selection_count") or d.get("records_matched")
            if sc is not None and str(sc).strip() != "":
                metadata_counts.update(extract_numbers_normalized(str(sc)))
            tr = d.get("total_rows_in_table") or d.get("total_rows") or d.get("records_examined")
            if tr is not None and str(tr).strip() != "":
                metadata_counts.update(extract_numbers_normalized(str(tr)))

        # Obtain structured results strictly from matching authoritative retained executions.
        # Citation result_value, secondary_value, and nested payloads must NOT introduce additional accepted results.
        matching_structured_items = []
        for c in citations:
            sel_hash = str(c.get("selection_hash", ""))
            for ev in retained_evidence_items:
                sp = getattr(ev, "structured_payload", {}) or (ev.get("structured_payload") if isinstance(ev, dict) else {}) or {}
                loc = getattr(ev, "locator", {}) or (ev.get("locator") if isinstance(ev, dict) else {}) or {}
                h = sp.get("selection_hash") or loc.get("selection_hash")
                if sel_hash and str(h) == sel_hash:
                    matching_structured_items.append(ev)
                    break
                elif not sel_hash and _ev_matches_citation(ev, c):
                    matching_structured_items.append(ev)
                    break

        if not matching_structured_items:
            for ev in retained_evidence_items:
                sp = getattr(ev, "structured_payload", {}) or (ev.get("structured_payload") if isinstance(ev, dict) else {}) or {}
                if sp:
                    matching_structured_items.append(ev)

        for ev in matching_structured_items:
            sp = getattr(ev, "structured_payload", {}) or (ev.get("structured_payload") if isinstance(ev, dict) else {}) or {}
            if isinstance(sp, dict):
                _inspect_d(sp)
            loc = getattr(ev, "locator", {}) or (ev.get("locator") if isinstance(ev, dict) else {}) or {}
            if isinstance(loc, dict):
                _inspect_d(loc)

        is_count_op = (
            "count" in operators
            or (isinstance(contract, dict) and str(contract.get("operator", "")).lower() == "count")
        )
        if is_count_op:
            computed_results = set(explicit_results)
            if not computed_results and metadata_counts:
                computed_results.update(metadata_counts)
        else:
            computed_results = set(explicit_results)

        if not computed_results:
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": "missing_retained_result_value",
                "citation_validation": cit_val,
                "faithfulness": None,
            }

        # Check claim-level contradiction against computed results
        contra = detect_contradiction_against_evidence(
            answer=str(answer),
            context="",
            computed_results=computed_results,
            contract=contract,
        )
        if contra:
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": contra,
                "citation_validation": cit_val,
                "faithfulness": None,
            }

        ans_nums = extract_numbers_normalized(str(answer))
        ans_years = {n for n in ans_nums if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099}
        ans_amounts = ans_nums - ans_years

        if not (ans_nums & computed_results):
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": f"claim_unsupported: answer does not assert computed result {computed_results}",
                "citation_validation": cit_val,
                "faithfulness": None,
            }
        if ans_amounts and not (ans_amounts & computed_results):
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": f"claim_unsupported: answer amounts {ans_amounts} do not match computed result {computed_results}",
                "citation_validation": cit_val,
                "faithfulness": None,
            }
        return {
            "is_grounded_correct": True,
            "grounded_correct": True,
            "reason": "grounded_structured_aggregate",
            "citation_validation": cit_val,
            "faithfulness": 1.0,
        }

    # Text retrieval claim support validation:
    # Build context strictly from authoritative retained evidence items
    auth_snippets = []
    for c in citations:
        matching_ev = None
        if retained_evidence_items:
            for ev in retained_evidence_items:
                if _ev_matches_citation(ev, c):
                    matching_ev = ev
                    break
        if matching_ev is not None:
            t = _get_ev_text(matching_ev).strip()
            if t:
                auth_snippets.append(t)
        else:
            snip = str(c.get("snippet") or c.get("text") or c.get("text_snippet") or "").strip()
            if snip:
                auth_snippets.append(snip)

    joined_context = " ".join(s for s in auth_snippets if s).strip()
    if not joined_context:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": "claim_unsupported: cited context snippets are empty",
            "citation_validation": cit_val,
            "faithfulness": None,
        }

    faith_score = compute_faithfulness(str(answer), joined_context)
    if faith_score is None or faith_score < 0.25:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": f"claim_unsupported: low citation faithfulness ({faith_score})",
            "citation_validation": cit_val,
            "faithfulness": faith_score,
        }

    # Check claim-level contradiction against authoritative retained context
    contra = detect_contradiction_against_evidence(
        answer=str(answer),
        context=joined_context,
        contract=contract,
    )
    if contra:
        return {
            "is_grounded_correct": False,
            "grounded_correct": False,
            "reason": contra,
            "citation_validation": cit_val,
            "faithfulness": faith_score,
        }

    # Verify numeric claims against context snippets or legitimate derived calculation targets
    ans_nums = extract_numbers_normalized(str(answer))
    context_nums = extract_numbers_normalized(joined_context)
    contract_targets = set()
    raw_operands = contract.get("operands", []) if contract and contract_type == "numeric" else []
    if contract and contract_type == "numeric":
        for tv in contract.get("target_values", []):
            contract_targets.update(extract_numbers_normalized(str(tv)))

    # A derived calculation is legitimate only when all necessary operands are present in the authoritative context.
    # Arbitrary context numbers must not unlock contract targets.
    valid_derived_targets = set()
    if contract_targets and raw_operands:
        all_operands_present = True
        for op_item in raw_operands:
            op_nums = extract_numbers_normalized(str(op_item))
            if not (op_nums & context_nums):
                all_operands_present = False
                break
        if all_operands_present:
            valid_derived_targets.update(contract_targets)

    ans_years = {n for n in ans_nums if len(n) == 4 and n.isdigit() and 1900 <= int(n) <= 2099}
    ans_amounts = ans_nums - ans_years

    # Filter out amounts that are explicitly negated in the answer (e.g. in legitimate premise refutations)
    asserted_amounts = {n for n in ans_amounts if not _is_entity_negated_in_text(n, str(answer))}
    asserted_years = {n for n in ans_years if not _is_entity_negated_in_text(n, str(answer))}

    if asserted_amounts:
        unsupported = asserted_amounts - context_nums - valid_derived_targets
        if unsupported:
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": f"claim_unsupported: numeric claims {unsupported} not supported by cited snippets",
                "citation_validation": cit_val,
                "faithfulness": faith_score,
            }
    if asserted_years and context_nums:
        if not (asserted_years & (context_nums | valid_derived_targets)):
            return {
                "is_grounded_correct": False,
                "grounded_correct": False,
                "reason": f"claim_unsupported: year claims {asserted_years} not supported by cited snippets",
                "citation_validation": cit_val,
                "faithfulness": faith_score,
            }

    return {
        "is_grounded_correct": True,
        "grounded_correct": True,
        "reason": "grounded_and_verified",
        "citation_validation": cit_val,
        "faithfulness": faith_score,
    }


def grade_canonical_answer_text(
    answer: str,
    expected_answer: str,
    contract_type: str = "semantic",
    contract: dict[str, Any] | None = None,
    ans_is_refusal: bool = False,
) -> bool:
    """Canonical benchmark answer text grading function.

    Grades text correctness across refusal, structured numeric, JSON schema,
    premise correction, and semantic contracts.
    """
    if contract_type == "refusal":
        return ans_is_refusal
    elif contract_type == "numeric":
        return grade_structured_numeric(answer, contract or {})
    elif contract_type == "json_schema":
        return grade_structured_json(answer, contract or {})
    elif contract_type == "premise_correction":
        return grade_structured_premise(answer, contract or {})
    else:
        if not expected_answer:
            return False
        relevancy = compute_answer_relevancy(answer, expected_answer)
        c_match = content_match(answer, expected_answer)
        return c_match and relevancy >= 0.40


def rescore_query_record_offline(
    record: dict[str, Any],
    recompute_text_correctness: bool = False,
) -> dict[str, Any]:
    """Reproduces grounded answer evaluation directly from a saved query record.

    Restores typed EvidenceItem instances from serialized retained_evidence_items,
    and runs evaluate_grounded_answer with all preserved evaluation inputs.
    When recompute_text_correctness is True, evaluates answer text correctness
    via the canonical grading path instead of relying on the saved text grade.
    """
    answer_text = str(record.get("answer", ""))
    citations = record.get("citations", [])
    contract_type = record.get("contract_type", "semantic")
    contract = record.get("contract")
    ans_is_refusal = bool(record.get("ans_is_refusal", record.get("is_refusal", False)))

    if recompute_text_correctness:
        expected_ans = record.get("expected_answer", "")
        is_ans_correct = grade_canonical_answer_text(
            answer=answer_text,
            expected_answer=expected_ans,
            contract_type=contract_type,
            contract=contract,
            ans_is_refusal=ans_is_refusal,
        )
    else:
        is_ans_correct = record.get("answer_text_correct", record.get("is_correct", False))
    expected_sources = record.get("expected_sources")
    expected_source_version = record.get("expected_source_version")

    # Restore typed EvidenceItem instances from serialized dictionaries
    raw_ev_items = record.get("retained_evidence_items")
    retained_evidence_items = (
        [restore_evidence_item(it) for it in raw_ev_items]
        if raw_ev_items is not None
        else None
    )

    context_chunk_ids = (
        record.get("compressed_context_chunk_ids")
        or record.get("context_chunk_ids")
    )
    ground_truth_chunk_ids = record.get("ground_truth_chunk_ids")
    is_infra_error = bool(record.get("is_infra_error", False))
    ans_is_refusal = bool(record.get("ans_is_refusal", record.get("is_refusal", False)))
    question_type = str(record.get("question_type", "retrieval"))

    outcome = {
        "structured_aggregate": bool(
            record.get("structured_aggregate")
            or question_type == "structured_aggregate"
            or contract_type == "structured_aggregate"
        ),
        "execution_type": record.get("execution_type", ""),
    }

    return evaluate_grounded_answer(
        answer=answer_text,
        citations=citations,
        is_answer_text_correct=is_ans_correct,
        contract_type=contract_type,
        contract=contract,
        expected_sources=expected_sources,
        expected_source_version=expected_source_version,
        retained_evidence_items=retained_evidence_items,
        context_chunk_ids=context_chunk_ids,
        ground_truth_chunk_ids=ground_truth_chunk_ids,
        is_infra_error=is_infra_error,
        ans_is_refusal=ans_is_refusal,
        question_type=question_type,
        outcome=outcome,
    )


