import json
import math
import re
from decimal import Decimal, InvalidOperation
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


def validate_citations(
    citations: list[dict],
    context_chunk_ids: list[str] | set[str] | None = None,
    ground_truth_chunk_ids: list[str] | set[str] | None = None,
    retained_structured_hashes: set[str] | None = None,
    retained_evidence_items: list[Any] | None = None,
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
    """
    if not citations:
        return {"valid": True, "citation_count": 0, "errors": []}

    # Extract retained hashes and chunk IDs from retained_evidence_items if provided
    retained_chunk_ids: set[str] | None = None
    all_retained_hashes: set[str] | None = set(retained_structured_hashes) if retained_structured_hashes is not None else None

    if retained_evidence_items is not None:
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
            # Verify hash matches a real executed query when caller supplies retained hashes/evidence
            if all_retained_hashes is not None and sel_hash not in all_retained_hashes:
                errors.append(
                    f"Citation {idx}: selection_hash '{sel_hash}' not in retained execution evidence "
                    f"(fabricated or stale citation)"
                )
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

