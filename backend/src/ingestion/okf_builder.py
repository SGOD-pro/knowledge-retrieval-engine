"""Bounded Open Knowledge Framework (OKF) extractor and builder.

Extracts canonical entity properties, curated table schemas, and verified facts
from text PDFs and tabular CSVs within strict token, model call, and deadline budgets.
Validates assertions against authorized source chunks and locators, rejecting
unsupported or hallucinated model claims before persistence.
"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Sequence

from config import settings
from src.db.okf_repo import (
    OkfRepositoryBase,
    create_dedicated_storage_config,
    disable_client_retries,
    get_okf_repo,
)
from src.schemas.contracts.okf import (
    OkfEntityRecord,
    OkfManifest,
    OkfPropertyAssertion,
    compute_assertion_id,
    compute_entity_id,
    compute_idempotency_key,
    normalize_entity_name,
    normalize_property_key,
)

logger = logging.getLogger(__name__)

# Supported model configurations with verified BPE token bounds
SUPPORTED_OKF_MODELS: set[str] = {
    "anthropic.claude-3-haiku-20240307-v1:0",
    "anthropic.claude-3-5-sonnet-20240620-v1:0",
    "anthropic.claude-3-sonnet-20240229-v1:0",
}

# Approximate characters per token for batch sizing
_CHARS_PER_TOKEN = 4

_STOP_WORDS = {
    "what", "when", "where", "who", "how", "tell", "is", "are", "the", "for",
    "under", "in", "on", "at", "to", "of", "with", "by", "according", "about",
    "from", "during", "before", "after", "per", "which", "whose", "does", "have",
    "show", "give", "can", "could", "should", "would", "an", "a", "this", "that",
    "these", "those", "all", "any", "some", "and", "or", "but", "while", "also",
}


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class DocumentExtractionBudget:
    """Enforces document level extraction bounds across batches, attempts, and retries."""
    workspace_id: str
    document_id: str
    document_version: str
    idempotency_key: str = ""
    max_calls: int = 2
    max_input_tokens: int = 4000
    max_call_input_tokens: int = 2000
    max_output_tokens: int = 1200
    max_call_output_tokens: int = 600
    max_elapsed_s: float = 10.0
    calls_made: int = 0
    input_tokens_used: int = 0
    output_tokens_used: int = 0
    started_at: float = field(default_factory=time.perf_counter)
    exhausted: bool = False
    exhaustion_reason: str | None = None

    def can_call(self) -> bool:
        if self.exhausted:
            return False
        if self.calls_made >= self.max_calls:
            self.exhausted = True
            self.exhaustion_reason = f"call_limit_reached ({self.calls_made}/{self.max_calls})"
            return False
        if self.input_tokens_used >= self.max_input_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"input_token_limit_reached ({self.input_tokens_used}/{self.max_input_tokens})"
            return False
        if self.output_tokens_used >= self.max_output_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"output_token_limit_reached ({self.output_tokens_used}/{self.max_output_tokens})"
            return False
        elapsed = time.perf_counter() - self.started_at
        if elapsed >= self.max_elapsed_s:
            self.exhausted = True
            self.exhaustion_reason = f"elapsed_time_limit_reached ({elapsed:.1f}s/{self.max_elapsed_s:.1f}s)"
            return False
        return True

    def reserve_call(self, prompt_tokens: int, max_output_tokens: int = 600) -> bool:
        """Reserve complete prompt and output budgets before provider calls."""
        if self.exhausted:
            return False
        if self.calls_made + 1 > self.max_calls:
            self.exhausted = True
            self.exhaustion_reason = f"call_limit_reached ({self.calls_made}/{self.max_calls})"
            return False
        if prompt_tokens > self.max_call_input_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"per_call_input_token_limit_exceeded ({prompt_tokens}/{self.max_call_input_tokens})"
            return False
        if max_output_tokens > self.max_call_output_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"per_call_output_token_limit_exceeded ({max_output_tokens}/{self.max_call_output_tokens})"
            return False
        if self.input_tokens_used + prompt_tokens > self.max_input_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"input_token_limit_exceeded ({self.input_tokens_used + prompt_tokens}/{self.max_input_tokens})"
            return False
        if self.output_tokens_used + max_output_tokens > self.max_output_tokens:
            self.exhausted = True
            self.exhaustion_reason = f"output_token_limit_exceeded ({self.output_tokens_used + max_output_tokens}/{self.max_output_tokens})"
            return False
        elapsed = time.perf_counter() - self.started_at
        if elapsed >= self.max_elapsed_s:
            self.exhausted = True
            self.exhaustion_reason = f"elapsed_time_limit_reached ({elapsed:.1f}s/{self.max_elapsed_s:.1f}s)"
            return False

        self.calls_made += 1
        self.input_tokens_used += prompt_tokens
        self.output_tokens_used += max_output_tokens
        return True

    def reconcile_call(
        self,
        reserved_prompt_tokens: int,
        reserved_output_tokens: int,
        actual_prompt_tokens: int,
        actual_output_tokens: int,
    ) -> None:
        """Reconcile actual token usage against upfront reservations."""
        self.input_tokens_used += (actual_prompt_tokens - reserved_prompt_tokens)
        self.output_tokens_used += (actual_output_tokens - reserved_output_tokens)
        if (
            self.calls_made >= self.max_calls
            or self.input_tokens_used >= self.max_input_tokens
            or self.output_tokens_used >= self.max_output_tokens
        ):
            self.exhausted = True

    def record_call(self, input_tokens: int, output_tokens: int) -> None:
        self.calls_made += 1
        self.input_tokens_used += input_tokens
        self.output_tokens_used += output_tokens
        if (
            self.calls_made >= self.max_calls
            or self.input_tokens_used >= self.max_input_tokens
            or self.output_tokens_used >= self.max_output_tokens
        ):
            self.exhausted = True


_MAX_BUDGET_LEDGER_ENTRIES = 1000
# Note on process boundary: _EXTRACTION_LOCKS and _EXTRACTION_BUDGETS coordinate concurrent
# threads within a single Python process. Multi process coordination across separate processes
# on the same host or across multiple hosts remains an open limitation for AC-3 pending
# distributed leasing or atomic conditional writes.
_EXTRACTION_LOCKS: dict[str, threading.Lock] = {}
_EXTRACTION_BUDGETS: dict[str, DocumentExtractionBudget] = {}
_GLOBAL_LOCK = threading.Lock()


def reset_budget_ledger() -> None:
    """Reset in memory extraction locks and budget ledger primarily for test isolation."""
    with _GLOBAL_LOCK:
        _EXTRACTION_BUDGETS.clear()
        _EXTRACTION_LOCKS.clear()
    reset_provider_controller()


def get_document_extraction_lock(idempotency_key: str) -> threading.Lock:
    with _GLOBAL_LOCK:
        if idempotency_key not in _EXTRACTION_LOCKS:
            if len(_EXTRACTION_LOCKS) >= _MAX_BUDGET_LEDGER_ENTRIES:
                unlocked_keys = [k for k, l in _EXTRACTION_LOCKS.items() if not l.locked()]
                if unlocked_keys:
                    del _EXTRACTION_LOCKS[unlocked_keys[0]]
            _EXTRACTION_LOCKS[idempotency_key] = threading.Lock()
        return _EXTRACTION_LOCKS[idempotency_key]


def get_or_create_document_budget(
    workspace_id: str,
    document_id: str,
    document_version: str,
    idempotency_key: str,
    max_calls: int = 2,
    max_input_tokens: int = 4000,
    max_call_input_tokens: int = 2000,
    max_output_tokens: int = 1200,
    max_call_output_tokens: int = 600,
    max_elapsed_s: float = 10.0,
    repo: Any | None = None,
) -> DocumentExtractionBudget:
    with _GLOBAL_LOCK:
        if idempotency_key in _EXTRACTION_BUDGETS:
            return _EXTRACTION_BUDGETS[idempotency_key]
        if len(_EXTRACTION_BUDGETS) >= _MAX_BUDGET_LEDGER_ENTRIES:
            # Prioritize evicting completed or exhausted budgets first; otherwise oldest overall
            exhausted_keys = [k for k, b in _EXTRACTION_BUDGETS.items() if b.exhausted]
            evict_key = exhausted_keys[0] if exhausted_keys else next(iter(_EXTRACTION_BUDGETS))
            del _EXTRACTION_BUDGETS[evict_key]

        # Preserve attempt accounting across restart and replay from durable repository manifest
        # Budget recovery distinguishes a missing reservation (get_manifest returns None)
        # from a storage read failure (get_manifest raises an exception); a failed read
        # must fail closed and mark the budget exhausted, rather than silently authorizing a fresh budget.
        prior_calls = 0
        prior_tokens = 0
        prior_exhausted = False
        prior_reason = None
        if repo is not None:
            try:
                # Strongly consistent read ensures recovery does not miss recently committed reservations
                existing_man = repo.get_manifest(
                    workspace_id,
                    document_id,
                    document_version,
                    consistent_read=True,
                )
                if existing_man is not None:
                    prior_calls = getattr(existing_man, "model_calls", 0)
                    prior_tokens = getattr(existing_man, "extraction_tokens", 0)
                    if existing_man.status == "failed" or prior_calls >= max_calls:
                        prior_exhausted = True
                        prior_reason = f"prior_recorded_attempts ({prior_calls}/{max_calls})"
            except Exception as e:
                logger.error("okf_builder.prior_manifest_storage_read_failed: %s", e)
                prior_exhausted = True
                prior_reason = f"storage_read_failed ({e})"

        budget = DocumentExtractionBudget(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            idempotency_key=idempotency_key,
            max_calls=max_calls,
            max_input_tokens=max_input_tokens,
            max_call_input_tokens=max_call_input_tokens,
            max_output_tokens=max_output_tokens,
            max_call_output_tokens=max_call_output_tokens,
            max_elapsed_s=max_elapsed_s,
            calls_made=prior_calls,
            input_tokens_used=prior_tokens // 2,
            output_tokens_used=prior_tokens - (prior_tokens // 2),
            exhausted=prior_exhausted,
            exhaustion_reason=prior_reason,
        )
        _EXTRACTION_BUDGETS[idempotency_key] = budget
        return budget


def is_value_grounded(property_value: str, chunk_text: str) -> bool:
    """Validate that property_value appears verbatim (or as normalized token sequence) in chunk."""
    if not property_value or not chunk_text:
        return False
    pv = property_value.strip().lower()
    ct = chunk_text.strip().lower()

    # Exact phrase with word boundaries to avoid false positives (e.g. 'cat' in 'communication')
    left_bound = r"\b" if re.match(r"^\w", pv) else r""
    right_bound = r"\b" if re.search(r"\w$", pv) else r""
    pattern = rf"{left_bound}{re.escape(pv)}{right_bound}"
    if re.search(pattern, ct):
        return True

    # Number normalization check with word boundaries (e.g. $4,191 -> 4191)
    pv_num = re.sub(r"(?<=\d),(?=\d)", "", pv)
    ct_num = re.sub(r"(?<=\d),(?=\d)", "", ct)
    if pv_num != pv or ct_num != ct:
        left_bound = r"\b" if re.match(r"^\w", pv_num) else r""
        right_bound = r"\b" if re.search(r"\w$", pv_num) else r""
        pattern_num = rf"{left_bound}{re.escape(pv_num)}{right_bound}"
        if re.search(pattern_num, ct_num):
            return True

    # Token sequence comparison (matches contiguous whole tokens)
    pv_tokens = re.findall(r"\w+", pv_num)
    ct_tokens = re.findall(r"\w+", ct_num)
    if not pv_tokens:
        return False
    n = len(pv_tokens)
    for i in range(len(ct_tokens) - n + 1):
        if ct_tokens[i : i + n] == pv_tokens:
            return True

    return False


_PROPERTY_SYNONYM_GROUPS: tuple[set[str], ...] = (
    {"employee", "employees", "staff", "headcount", "workforce", "personnel", "worker", "workers", "people"},
    {"revenue", "revenues", "sales", "turnover", "topline", "receipts"},
    {"profit", "profits", "income", "earnings", "ebitda", "gain", "gains", "margin"},
    {"operating", "operations", "operational"},
    {"founded", "founding", "established", "started", "incorporated", "created", "origin", "originated"},
    {"headquarters", "headquarter", "hq", "based", "located", "location"},
    {"ceo", "chief executive", "chief executive officer", "president"},
    {"cfo", "chief financial officer"},
    {"cto", "chief technology officer"},
    {"price", "cost", "costs", "valuation", "worth", "amount", "fee", "expense", "expenses"},
    {"acquisition", "acquired", "purchased", "bought", "buyout", "merger", "takeover"},
    {"customer", "customers", "client", "clients", "user", "users", "subscribers", "subscriber"},
    {"debt", "debts", "liabilities", "liability", "borrowing", "borrowings", "loans", "loan"},
    {"asset", "assets", "holdings", "reserves", "cash"},
    {"dividend", "dividends", "yield", "payout"},
    {"shares", "stock", "equity"},
    {"industry", "sector", "field", "domain", "vertical"},
)

_GENERIC_METRIC_MODIFIERS: set[str] = {
    "total", "net", "gross", "annual", "quarterly", "fiscal", "count", "number",
    "rate", "ratio", "level", "value", "year", "date", "percent", "percentage",
    "amount", "figure", "item", "category",
}


def _normalize_stem(w: str) -> str:
    s = w.lower()
    if s.endswith("ies") and len(s) > 4:
        return s[:-3] + "y"
    if s.endswith("es") and len(s) > 4:
        return s[:-2]
    if s.endswith("s") and not s.endswith("ss") and len(s) > 3:
        return s[:-1]
    if s.endswith("ing") and len(s) > 5:
        return s[:-3]
    if s.endswith("ed") and len(s) > 4:
        return s[:-2]
    return s


def is_property_grounded(property_key: str, text: str) -> bool:
    """Validate that property_key semantic tokens or domain synonyms are grounded in text."""
    if not property_key or not text:
        return False
    norm = re.sub(r"([a-z])([A-Z])", r"\1 \2", property_key).lower()
    norm = re.sub(r"[-_]+", " ", norm)
    raw_tokens = [t for t in re.findall(r"\w+", norm) if t not in _STOP_WORDS and len(t) > 1]
    if not raw_tokens:
        return property_key.strip().lower() in text.lower()

    substantive = [t for t in raw_tokens if t not in _GENERIC_METRIC_MODIFIERS]
    tokens_to_match = substantive if substantive else raw_tokens

    text_lower = text.lower()
    text_tokens = re.findall(r"\w+", text_lower)
    text_token_set = set(text_tokens)
    text_stems = {_normalize_stem(t) for t in text_tokens}

    if norm in text_lower:
        return True

    for target in tokens_to_match:
        if target in text_token_set:
            return True
        target_stem = _normalize_stem(target)
        if target_stem in text_stems:
            return True
        for group in _PROPERTY_SYNONYM_GROUPS:
            if target in group or target_stem in group:
                for syn in group:
                    if " " in syn:
                        if syn in text_lower:
                            return True
                    elif syn in text_token_set or _normalize_stem(syn) in text_stems:
                        return True

    return False


def _extract_property_terms(property_key: str) -> tuple[set[str], set[str]]:
    """Return (target_synonyms, competing_property_terms) for a given property key."""
    norm = re.sub(r"([a-z])([A-Z])", r"\1 \2", property_key).lower()
    norm = re.sub(r"[-_]+", " ", norm)
    raw_tokens = [t for t in re.findall(r"\w+", norm) if t not in _STOP_WORDS and len(t) > 1]
    substantive = [t for t in raw_tokens if t not in _GENERIC_METRIC_MODIFIERS]
    match_tokens = substantive if substantive else raw_tokens

    target_synonyms: set[str] = set(match_tokens)
    target_groups: list[set[str]] = []

    for t in match_tokens:
        t_stem = _normalize_stem(t)
        for group in _PROPERTY_SYNONYM_GROUPS:
            if t in group or t_stem in group:
                target_synonyms.update(group)
                target_groups.append(group)

    competing_terms: set[str] = set()
    for group in _PROPERTY_SYNONYM_GROUPS:
        if group not in target_groups:
            competing_terms.update(group)

    metric_property_terms = {
        "profit", "profits", "income", "earnings", "ebitda", "revenue", "revenues",
        "sales", "turnover", "loss", "losses", "expense", "expenses", "cost", "costs",
        "debt", "debts", "liabilities", "asset", "assets", "headcount", "employee",
        "employees", "margin", "valuation", "price", "dividend", "shares", "equity",
    }
    target_stems = {_normalize_stem(s) for s in target_synonyms}
    for m in metric_property_terms:
        if m not in target_synonyms and _normalize_stem(m) not in target_stems:
            competing_terms.add(m)

    return target_synonyms, competing_terms


def _span_dist(s1: int, e1: int, s2: int, e2: int) -> int:
    """Compute character distance between two spans (0 if overlapping or contiguous)."""
    if e1 <= s2:
        return s2 - e1
    if e2 <= s1:
        return s1 - e2
    return 0


def _find_term_occurrences(terms: set[str], text: str) -> list[tuple[int, int, str]]:
    """Find character spans (start, end, term) of matched terms in text."""
    text_lower = text.lower()
    occurrences: list[tuple[int, int, str]] = []
    for term in terms:
        pattern = rf"\b{re.escape(term)}\b"
        for match in re.finditer(pattern, text_lower):
            occurrences.append((match.start(), match.end(), term))
    occurrences.sort(key=lambda x: x[0])
    return occurrences


def validate_assertion_grounding(
    entity_name: str,
    property_key: str,
    property_value: str,
    chunk_text: str,
) -> tuple[bool, str]:
    """Validate entity property value relationship in chunk, rejecting unsupported or ambiguous claims.
    
    Verifies that:
    1. The property value occurs in the chunk text.
    2. The asserted entity occurs in the chunk text.
    3. The property key concept is grounded in the chunk text and specifically in the context of the value.
    4. The value is syntactically or relationally attributed to the asserted entity rather than
       another competing entity in the chunk.
    """
    if not property_value or not chunk_text or not entity_name:
        return False, "missing_input"

    # Check 1: Value grounding
    if not is_value_grounded(property_value, chunk_text):
        return False, "value_not_grounded"

    # Check 2: Entity grounding in chunk
    if not is_value_grounded(entity_name, chunk_text):
        return False, "entity_not_in_chunk"

    # Check 3: Property key grounding in chunk
    if not is_property_grounded(property_key, chunk_text):
        return False, "unsupported_property"

    # Check 4: Relational binding between entity, property, and value
    ent_lower = entity_name.strip().lower()
    val_lower = property_value.strip().lower()

    # Split chunk into sentences
    raw_sentences = [s.strip() for s in re.split(r"(?<=[.!?\n])\s+", chunk_text) if s.strip()]
    sentences = raw_sentences if raw_sentences else [chunk_text]

    # Find sentences containing the property value
    val_sentences = [s for s in sentences if is_value_grounded(property_value, s)]
    if not val_sentences:
        val_sentences = sentences

    # Verify that at least one sentence containing the value supports this property
    supporting_val_sentences = [s for s in val_sentences if is_property_grounded(property_key, s)]
    if not supporting_val_sentences:
        if len(sentences) > 1:
            return False, "unsupported_relationship"
        supporting_val_sentences = val_sentences

    # Identify potential competing entities in the chunk text
    prop_norm = property_key.strip().lower().replace("_", " ")
    prop_tokens = set(re.findall(r"\w+", prop_norm))
    metric_terms = {
        "total", "net", "gross", "operating", "fiscal", "annual", "quarterly",
        "average", "median", "minimum", "maximum", "estimated", "reported",
        "adjusted", "year", "month", "period", "table", "summary", "column",
        "figure", "item", "category", "number", "value", "percent", "percentage",
        "rate", "ratio", "score", "index", "level", "cost", "price", "amount",
        "revenue", "income", "profit", "loss", "expense", "expenses", "ebitda",
        "margin", "cash", "debt", "equity", "asset", "assets", "liability", "liabilities",
    }
    corp_indicators = (
        "inc", "corp", "llc", "ltd", "group", "bank", "technologies", "company",
        "co", "labs", "partners", "holdings",
    )

    candidate_entities = re.findall(r"\b[A-Z][a-zA-Z0-9]+(?:\s+[A-Z][a-zA-Z0-9]+)*\b", chunk_text)
    other_entities: set[str] = set()
    for cand in candidate_entities:
        cand_norm = cand.strip().lower()
        if cand_norm == ent_lower or cand_norm in ent_lower or ent_lower in cand_norm:
            continue
        if cand_norm in _STOP_WORDS or len(cand_norm) <= 3:
            continue
        cand_words = set(re.findall(r"\w+", cand_norm))
        # Exclude candidates that overlap with property key tokens or common financial metric terms
        if cand_words.intersection(prop_tokens) or cand_words.issubset(metric_terms):
            continue
        has_corp = any(ci in cand_norm for ci in corp_indicators)
        is_mid_sentence = bool(re.search(rf"(?<![.!?\n]\s)(?<!^)\b{re.escape(cand)}\b", chunk_text))
        if len(cand_words) > 1 or has_corp or is_mid_sentence:
            other_entities.add(cand_norm)

    target_synonyms, competing_property_terms = _extract_property_terms(property_key)
    has_ambiguous_binding = False
    valid_sentence_found = False

    for sent in supporting_val_sentences:
        sent_lower = sent.lower()

        # Step 4a: Competing property relational validation within sentence
        target_prop_mentions = _find_term_occurrences(target_synonyms, sent)
        competing_prop_mentions = _find_term_occurrences(competing_property_terms, sent)

        val_occurrences = _find_term_occurrences({val_lower}, sent)
        if not val_occurrences:
            val_idx = sent_lower.find(val_lower)
            if val_idx != -1:
                val_occurrences = [(val_idx, val_idx + len(val_lower), val_lower)]
            else:
                val_toks = re.findall(r"\w+", val_lower)
                if val_toks:
                    v_tok = val_toks[0]
                    v_idx = sent_lower.find(v_tok)
                    if v_idx != -1:
                        val_occurrences = [(v_idx, v_idx + len(v_tok), v_tok)]

        # Check if any value occurrence in this sentence is validly bound to the target property
        has_valid_property_binding = False

        if not competing_prop_mentions:
            has_valid_property_binding = True
        else:
            # We have competing properties in this sentence!
            clause_delims = [0] + [m.start() for m in re.finditer(r"\b(?:and|but|while|whereas|although|however|meanwhile)\b|[;,]", sent, flags=re.IGNORECASE)] + [len(sent)]
            clause_delims = sorted(set(clause_delims))
            clauses = [(clause_delims[i], clause_delims[i+1]) for i in range(len(clause_delims) - 1)]

            for v_start, v_end, _ in val_occurrences:
                # 1. Clause analysis
                v_clause = next(((c_s, c_e) for c_s, c_e in clauses if c_s <= v_start < c_e), (0, len(sent)))
                clause_competing = any(c_s >= v_clause[0] and c_e <= v_clause[1] for c_s, c_e, _ in competing_prop_mentions)
                clause_target = any(p_s >= v_clause[0] and p_e <= v_clause[1] for p_s, p_e, _ in target_prop_mentions)

                if clause_competing and not clause_target:
                    # Value is in a clause with a competing property and without the target property
                    continue

                if clause_target and not clause_competing:
                    # Value is in a distinct clause containing only the target property
                    has_valid_property_binding = True
                    break

                # 2. Proximity and intervening property check when both or neither in clause
                dist_target = min(_span_dist(v_start, v_end, p_s, p_e) for p_s, p_e, _ in target_prop_mentions) if target_prop_mentions else 999999
                dist_comp = min(_span_dist(v_start, v_end, c_s, c_e) for c_s, c_e, _ in competing_prop_mentions)

                # Competing property is strictly closer
                if dist_comp < dist_target:
                    continue

                # Competing property intervenes between target property and value
                closest_target = min(target_prop_mentions, key=lambda p: _span_dist(v_start, v_end, p[0], p[1])) if target_prop_mentions else None
                intervening = False
                if closest_target is not None:
                    p_min, p_max = min(closest_target[0], v_start), max(closest_target[1], v_end)
                    for c_s, c_e, _ in competing_prop_mentions:
                        if p_min < c_s < p_max:
                            intervening = True
                            break
                if intervening:
                    continue

                if dist_comp == dist_target:
                    has_ambiguous_binding = True
                    continue

                has_valid_property_binding = True
                break

        if not has_valid_property_binding:
            continue

        # Step 4b: Entity attribution within sentence
        ent_in_sent = is_value_grounded(entity_name, sent)
        competing_in_sent = [oe for oe in other_entities if is_value_grounded(oe, sent)]

        if ent_in_sent and not competing_in_sent:
            return True, "verified"

        if not ent_in_sent and competing_in_sent:
            # The value is in a sentence that mentions another entity, but NOT the asserted entity
            return False, "wrong_entity_attribution"

        if ent_in_sent and competing_in_sent:
            # Both asserted entity and competing entity appear in the same sentence.
            # Measure character index proximity to the property value
            val_idx = val_occurrences[0][0] if val_occurrences else sent_lower.find(val_lower)
            ent_idx = sent_lower.find(ent_lower)
            if ent_idx == -1:
                ent_toks = re.findall(r"\w+", ent_lower)
                if ent_toks:
                    ent_idx = sent_lower.find(ent_toks[0])

            ent_dist = abs(val_idx - ent_idx) if (val_idx != -1 and ent_idx != -1) else 999999

            other_closer = False
            for oe in competing_in_sent:
                oe_idx = sent_lower.find(oe)
                if oe_idx != -1 and val_idx != -1:
                    oe_dist = abs(val_idx - oe_idx)
                    if oe_dist < ent_dist:
                        other_closer = True
                        break

            if other_closer:
                return False, "wrong_entity_attribution"

            return True, "verified"

        valid_sentence_found = True

    if not valid_sentence_found:
        if has_ambiguous_binding:
            return False, "ambiguous_relationship"
        return False, "unsupported_relationship"

    # If entity and value are in different sentences and other entities exist in the chunk
    if other_entities:
        return False, "ambiguous_relationship"

    return True, "verified"


def count_request_tokens(
    system_prompt: str,
    user_prompt: str,
    model_id: str | None = None,
) -> int:
    """Compute a defensible conservative upper bound on tokens for the complete provider request.

    Budgets the complete request including system instructions, user content blocks,
    XML tags, punctuation, and envelope framing overhead.

    Mathematical Evidence for Supported Models:
    - Supported Models: Claude 3 Haiku, Claude 3.5 Sonnet, and Claude 3 Sonnet.
    - Tokenizer: Byte-level Byte Pair Encoding (BPE).
    - Lower Bound Invariant: In byte-level BPE vocabularies where base tokens represent
      individual UTF-8 byte values (0 to 255), each token represents at least 1 byte of input text.
      Merged tokens represent 2 or more bytes. Therefore, for all UTF-8 text T,
      the token count N satisfies: N_tokens <= |T|_bytes.
    - Proven Text Upper Bound: Taking byte length |T|_bytes guarantees that text token count cannot
      be underestimated, even for pathological high density single character or punctuation
      sequences where character heuristics (such as len // 4) fail by more than 2x.

    Documented Assumptions and Open Evidence Requirements:
    - Text Payload: The text upper bound proof relies on the byte-level BPE invariant N_tokens <= |T|_bytes.
      For non-BPE tokenizers, synthetic expansion models, or unverified model families, this is unverified.
    - Framing Allowance: The 32 token envelope framing allowance covers Bedrock Converse API JSON structure,
      system content blocks, role indicators, and internal turn delimiters. This 32 token buffer is an unverified
      heuristic assumption that still requires empirical provider and model tokenizer evidence from AWS Bedrock.
    - Fail Closed Enforcement: Any model_id outside SUPPORTED_OKF_MODELS fails closed by raising ValueError,
      preventing unbudgeted provider calls.
    """
    if model_id is not None and model_id not in SUPPORTED_OKF_MODELS:
        raise ValueError(f"Unsupported model configuration: {model_id}")

    full_text = f"{system_prompt}\n\n{user_prompt}"
    byte_bound = len(full_text.encode("utf-8"))
    envelope_overhead = 32
    return byte_bound + envelope_overhead


class OkfProviderConcurrencyController:
    """Global concurrency controller for OKF provider extraction calls across documents.

    Enforces:
    - Bounded concurrency across documents (default max 2 concurrent Bedrock calls).
    - Bounded admission queue with immediate load shedding (default max 4 queued calls).
    - Capacity retention: when a caller's document deadline expires (caller timeout),
      the underlying worker slot in the semaphore and thread pool is retained until the
      underlying provider call finishes, preventing caller timeouts from flooding Bedrock.
    """

    def __init__(
        self,
        max_concurrency: int = 2,
        max_queue: int = 4,
    ) -> None:
        self.max_concurrency = max_concurrency
        self.max_queue = max_queue
        self._active_workers = 0
        self._queued_callers = 0
        self._semaphore = threading.Semaphore(self.max_concurrency)
        self._lock = threading.Lock()
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_concurrency,
            thread_name_prefix="okf_provider_worker",
        )

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active_workers

    @property
    def queued_count(self) -> int:
        with self._lock:
            return self._queued_callers

    def run_provider_call(
        self,
        func: Callable[..., Any],
        *args: Any,
        timeout_s: float = 10.0,
        **kwargs: Any,
    ) -> tuple[Any, str]:
        """Execute blocking provider call with capacity retention and caller timeout enforcement.

        Returns (result, status) where status is:
        - "completed": underlying call returned successfully
        - "caller_timeout": caller deadline expired while waiting for slot or completion
        - "admission_rejected": queue is saturated
        - "call_failed": underlying call raised an exception
        """
        with self._lock:
            if self._queued_callers >= self.max_queue:
                logger.warning(
                    "okf.provider_admission_rejected queued=%d max=%d",
                    self._queued_callers,
                    self.max_queue,
                )
                return None, "admission_rejected"
            self._queued_callers += 1

        t0 = time.perf_counter()
        acquired = self._semaphore.acquire(timeout=timeout_s)
        if not acquired:
            with self._lock:
                self._queued_callers -= 1
            logger.warning("okf.provider_timeout stage=queue_admission timeout_s=%.2f", timeout_s)
            return None, "caller_timeout"

        with self._lock:
            self._queued_callers -= 1
            self._active_workers += 1

        elapsed = time.perf_counter() - t0
        remaining_s = max(0.001, timeout_s - elapsed)

        outcome: dict[str, Any] = {"result": None, "error": None, "status": "running"}
        call_done = threading.Event()

        def _worker_task() -> None:
            try:
                outcome["result"] = func(*args, **kwargs)
                outcome["status"] = "completed"
            except Exception as exc:
                outcome["error"] = exc
                outcome["status"] = "call_failed"
            finally:
                with self._lock:
                    self._active_workers = max(0, self._active_workers - 1)
                self._semaphore.release()
                call_done.set()

        self._executor.submit(_worker_task)

        finished = call_done.wait(timeout=remaining_s)
        if finished:
            if outcome["error"] is not None:
                raise outcome["error"]
            return outcome["result"], outcome["status"]

        # Caller timeout: capacity in self._semaphore remains retained
        # until _worker_task actually finishes in the background!
        logger.warning("okf.provider_timeout stage=call_execution timeout_s=%.2f", timeout_s)
        return None, "caller_timeout"


_PROVIDER_CONCURRENCY_CONTROLLER: OkfProviderConcurrencyController | None = None


def get_okf_provider_controller() -> OkfProviderConcurrencyController:
    """Return the global shared OKF provider concurrency controller."""
    global _PROVIDER_CONCURRENCY_CONTROLLER
    with _GLOBAL_LOCK:
        if _PROVIDER_CONCURRENCY_CONTROLLER is None:
            max_c = int(getattr(settings, "OKF_PROVIDER_MAX_CONCURRENCY", 2))
            max_q = int(getattr(settings, "OKF_PROVIDER_MAX_QUEUE", 4))
            _PROVIDER_CONCURRENCY_CONTROLLER = OkfProviderConcurrencyController(
                max_concurrency=max_c,
                max_queue=max_q,
            )
        return _PROVIDER_CONCURRENCY_CONTROLLER


def reset_provider_controller() -> None:
    """Reset the global provider concurrency controller for test isolation."""
    global _PROVIDER_CONCURRENCY_CONTROLLER
    with _GLOBAL_LOCK:
        if _PROVIDER_CONCURRENCY_CONTROLLER is not None:
            try:
                _PROVIDER_CONCURRENCY_CONTROLLER._executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            _PROVIDER_CONCURRENCY_CONTROLLER = None


class OkfBuilder:
    """Orchestrates bounded OKF fact extraction, grounding validation, and persistence."""

    def __init__(
        self,
        repo: OkfRepositoryBase | None = None,
        bedrock_client: Any | None = None,
        provider_controller: OkfProviderConcurrencyController | None = None,
    ) -> None:
        self.repo = repo or get_okf_repo()
        self._bedrock_client = bedrock_client
        self.provider_controller = provider_controller or get_okf_provider_controller()

    def _get_bedrock_client(self) -> Any:
        if self._bedrock_client is not None:
            return self._bedrock_client

        env = getattr(settings, "ENVIRONMENT", "dev").lower()
        provider = getattr(settings, "MODEL_PROVIDER", "dev").lower()
        if env == "test" or provider == "test":
            return None

        import boto3
        from aws.infra import get_client

        cfg = create_dedicated_storage_config(connect_timeout=3.0, read_timeout=7.0)
        try:
            client = get_client("bedrock-runtime", config=cfg)
        except Exception:
            session = boto3.Session()
            client = session.client(
                "bedrock-runtime",
                region_name=getattr(settings, "AWS_REGION", "us-east-1"),
                config=cfg,
            )
        disable_client_retries(client)
        self._bedrock_client = client
        return self._bedrock_client

    def extract_and_persist(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        format: str,
        chunks: Sequence[Any],
        table_name: str | None = None,
        table_store: Any | None = None,
        force_reextract: bool = False,
    ) -> OkfManifest:
        """Extract facts and persist manifest enforcing strict resource bounds and ADR-001.
        
        Guarantees:
        - Tabular CSV: 0 model calls, 0 token cost, curated table schema extraction.
        - Text PDF: Max 2 calls, max 4000 input tokens, max 1200 output tokens, 10s deadline.
        - Chunk Grounding: Verbatim text validation; unsupported claims are rejected.
        - Idempotency: Replaying an existing version reuses assertions with 0 model calls.
        - Fail open: Failures return a manifest with status='failed' without raising errors.
        """
        idempotency_key = compute_idempotency_key(workspace_id, document_id, document_version)
        timestamp = _now_utc()

        doc_lock = get_document_extraction_lock(idempotency_key)
        with doc_lock:
            # Idempotency check: Reuse existing manifest if ready and not forced
            try:
                if not force_reextract and self.repo.has_ready_okf(workspace_id, document_id, document_version):
                    existing_man = self.repo.get_manifest(
                        workspace_id,
                        document_id,
                        document_version,
                        consistent_read=True,
                    )
                    if existing_man:
                        logger.info(
                            "okf_builder.idempotent_replay ws=%s doc=%s ver=%s assertions=%d",
                            workspace_id,
                            document_id,
                            document_version,
                            len(existing_man.assertions),
                        )
                        return existing_man
            except Exception as ready_err:
                logger.error("okf_builder.idempotency_lookup_failed: %s", ready_err)
                failed_manifest = OkfManifest(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    entities=(),
                    assertions=(),
                    rejected_assertions_count=0,
                    status="failed",
                    extraction_tokens=0,
                    model_calls=0,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                )
                return failed_manifest

            budget = get_or_create_document_budget(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                idempotency_key=idempotency_key,
                max_calls=int(getattr(settings, "OKF_MAX_MODEL_CALLS_PER_DOC", 2)),
                max_elapsed_s=10.0,
                repo=self.repo,
            )

            # Enforce single document extraction budget across replays and attempts (AC-3)
            # Replays and concurrent ingestion must not reset that budget silently
            if not force_reextract and budget.exhausted:
                logger.warning(
                    "okf_builder.budget_exhausted_replay ws=%s doc=%s ver=%s prior_calls=%d reason=%s (not resetting budget)",
                    workspace_id,
                    document_id,
                    document_version,
                    budget.calls_made,
                    budget.exhaustion_reason,
                )
                try:
                    existing_man = self.repo.get_manifest(
                        workspace_id,
                        document_id,
                        document_version,
                        consistent_read=True,
                    )
                    if existing_man:
                        return existing_man
                except Exception:
                    pass
                failed_manifest = OkfManifest(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    entities=(),
                    assertions=(),
                    rejected_assertions_count=0,
                    status="failed",
                    extraction_tokens=budget.input_tokens_used + budget.output_tokens_used,
                    model_calls=budget.calls_made,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                )
                return failed_manifest

            fmt = (format or "").lower().replace(".", "")

            try:
                if fmt == "csv":
                    return self._extract_csv(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        chunks=chunks,
                        table_name=table_name,
                        table_store=table_store,
                        idempotency_key=idempotency_key,
                        timestamp=timestamp,
                    )
                elif fmt == "pdf":
                    return self._extract_pdf(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        chunks=chunks,
                        idempotency_key=idempotency_key,
                        timestamp=timestamp,
                        budget=budget,
                    )
                else:
                    # Unsupported format for factual entity extraction (DOCX/PPTX) -> return empty manifest
                    manifest = OkfManifest(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=document_version,
                        entities=(),
                        assertions=(),
                        rejected_assertions_count=0,
                        status="empty",
                        extraction_tokens=0,
                        model_calls=0,
                        idempotency_key=idempotency_key,
                        created_at=timestamp,
                    )
                    self.repo.save_manifest(workspace_id, manifest)
                    return manifest

            except Exception as exc:
                logger.warning(
                    "okf_builder.extraction_failed ws=%s doc=%s ver=%s error=%s (failing open per ADR-001)",
                    workspace_id,
                    document_id,
                    document_version,
                    exc,
                )
                failed_manifest = OkfManifest(
                    workspace_id=workspace_id,
                    document_id=document_id,
                    document_version=document_version,
                    entities=(),
                    assertions=(),
                    rejected_assertions_count=0,
                    status="failed",
                    extraction_tokens=budget.input_tokens_used + budget.output_tokens_used if 'budget' in locals() else 0,
                    model_calls=budget.calls_made if 'budget' in locals() else 1,
                    idempotency_key=idempotency_key,
                    created_at=timestamp,
                )
                try:
                    self.repo.save_manifest(workspace_id, failed_manifest)
                except Exception:
                    pass
                return failed_manifest

    def _extract_csv(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        chunks: Sequence[Any],
        table_name: str | None,
        table_store: Any | None,
        idempotency_key: str,
        timestamp: str,
    ) -> OkfManifest:
        """Deterministic curated table schema and column header extraction with 0 model calls."""
        t_name = table_name or document_id
        ent_name = t_name
        norm_name = normalize_entity_name(ent_name)
        ent_id = compute_entity_id(workspace_id, document_id, norm_name)

        entity = OkfEntityRecord(
            workspace_id=workspace_id,
            entity_id=ent_id,
            entity_name=ent_name,
            normalized_name=norm_name,
            entity_type="table",
            document_id=document_id,
            document_version=document_version,
            created_at=timestamp,
        )

        assertions: list[OkfPropertyAssertion] = []
        columns_found: list[tuple[str, str]] = []  # (col_name, data_type)

        # 1. Attempt to inspect TableStore for schema definition
        if table_store is not None:
            try:
                t_meta = table_store.get_table(t_name, workspace_id)
                if t_meta and hasattr(t_meta, "schema") and t_meta.schema:
                    for col in t_meta.schema.columns:
                        dtype_str = (
                            col.inferred_dtype.value
                            if hasattr(col.inferred_dtype, "value")
                            else str(col.inferred_dtype)
                        )
                        columns_found.append((col.name, dtype_str))
            except Exception as e:
                logger.debug("TableStore schema inspection skipped: %s", e)

        # 2. If TableStore didn't supply columns, inspect chunks for metadata headers
        if not columns_found and chunks:
            for ch in chunks:
                meta = getattr(ch, "metadata", {}) or {}
                hdrs = meta.get("headers")
                if hdrs and isinstance(hdrs, (list, tuple)):
                    for h in hdrs:
                        if h and str(h).strip():
                            columns_found.append((str(h).strip(), "string"))
                    break

        # Fallback default chunk identifier for table facts
        header_chunk_id = str(chunks[0].id) if chunks else f"{document_id}:csv:header"

        # 3. Build property assertions for columns
        for col_name, dtype in columns_found:
            prop_key = f"column:{col_name}"
            prop_val = dtype
            ass_id = compute_assertion_id(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entity_id=ent_id,
                property_key=prop_key,
                property_value=prop_val,
                source_chunk_id=header_chunk_id,
            )
            assertions.append(
                OkfPropertyAssertion(
                    assertion_id=ass_id,
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    property_key=prop_key,
                    property_value=prop_val,
                    value_type="schema_type",
                    source_chunk_id=header_chunk_id,
                    document_id=document_id,
                    document_version=document_version,
                    page_number=None,
                    table_name=t_name,
                    column_name=col_name,
                    confidence_score=1.0,
                    validation_status="verified",
                    created_at=timestamp,
                )
            )

        manifest = OkfManifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            entities=(entity,),
            assertions=tuple(assertions),
            rejected_assertions_count=0,
            status="ready" if assertions else "empty",
            extraction_tokens=0,
            model_calls=0,
            idempotency_key=idempotency_key,
            created_at=timestamp,
        )

        self.repo.save_manifest(workspace_id, manifest)
        logger.info(
            "okf_builder.csv_ready ws=%s doc=%s ver=%s columns=%d (0 tokens, 0 model calls)",
            workspace_id,
            document_id,
            document_version,
            len(assertions),
        )
        return manifest

    def _extract_pdf(
        self,
        workspace_id: str,
        document_id: str,
        document_version: str,
        chunks: Sequence[Any],
        idempotency_key: str,
        timestamp: str,
        budget: DocumentExtractionBudget | None = None,
    ) -> OkfManifest:
        """Bounded Bedrock fact extraction for text PDFs with chunk grounding validation."""
        if budget is None:
            budget = get_or_create_document_budget(
                workspace_id, document_id, document_version, idempotency_key, repo=self.repo,
            )

        if not chunks:
            manifest = OkfManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entities=(),
                assertions=(),
                rejected_assertions_count=0,
                status="empty",
                extraction_tokens=0,
                model_calls=0,
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )
            self.repo.save_manifest(workspace_id, manifest)
            return manifest

        # Build authorized chunk lookup map: source_chunk_id -> chunk
        authorized_chunks = {str(c.id): c for c in chunks}

        # Select introductory and high density chunks up to budget
        # AC-3: max 2 calls, max 2000 input tokens per call, max 4000 total input tokens
        # Safe byte sizing ensures request (system + user + framing) cannot exceed 2000 tokens
        max_calls = budget.max_calls
        max_prompt_bytes = 1300

        batches: list[list[Any]] = []
        current_batch: list[Any] = []
        current_bytes = 0

        for ch in chunks:
            ch_text = getattr(ch, "text", "") or ""
            ch_bytes = min(len(ch_text.encode("utf-8")), max_prompt_bytes)
            if current_bytes + ch_bytes > max_prompt_bytes and current_batch:
                batches.append(current_batch)
                current_batch = []
                current_bytes = 0
                if len(batches) >= max_calls:
                    break
            current_batch.append(ch)
            current_bytes += ch_bytes

        if current_batch and len(batches) < max_calls:
            batches.append(current_batch)

        raw_candidates: list[dict[str, Any]] = []
        client = self._get_bedrock_client()

        for batch_idx, batch in enumerate(batches):
            if not budget.can_call():
                logger.warning(
                    "okf_builder.budget_stopped batch=%d reason=%s",
                    batch_idx, budget.exhaustion_reason,
                )
                break

            prompt_text, text_stats = self._invoke_bedrock_extraction(
                client=client,
                chunks=batch,
                workspace_id=workspace_id,
                document_id=document_id,
                budget=budget,
            )

            if prompt_text:
                parsed = self._parse_model_response(prompt_text)
                raw_candidates.extend(parsed)

        # Grounding validation pass (AC-2)
        valid_entities_map: dict[str, OkfEntityRecord] = {}
        valid_assertions: list[OkfPropertyAssertion] = []
        rejected_count = 0

        for item in raw_candidates:
            ent_name = item.get("entity_name") or item.get("concept") or ""
            prop_key = item.get("property_key") or item.get("property") or ""
            prop_val = item.get("property_value") or item.get("value") or ""
            chunk_id = item.get("source_chunk_id") or item.get("chunk_id") or ""
            val_type = item.get("value_type", "string")

            if not ent_name or not prop_key or not prop_val or not chunk_id:
                rejected_count += 1
                continue

            # Check 1: Authorized source chunk membership (AC-2)
            chunk = authorized_chunks.get(str(chunk_id))
            if chunk is None:
                logger.debug(
                    "okf_builder.rejection_unauthorized_chunk chunk_id=%s doc=%s",
                    chunk_id,
                    document_id,
                )
                rejected_count += 1
                continue

            # Check 2: Locator verification (AC-2)
            page_num = getattr(chunk, "page_number", None)
            loc_ref = getattr(chunk, "location_reference", None)
            if page_num is None and loc_ref is None:
                logger.debug("okf_builder.rejection_missing_locator chunk_id=%s", chunk_id)
                rejected_count += 1
                continue

            # Check 3: Entity property value relationship grounding verification (AC-2)
            ch_text = getattr(chunk, "text", "") or ""
            is_valid, reason = validate_assertion_grounding(ent_name, prop_key, prop_val, ch_text)
            if not is_valid:
                logger.debug(
                    "okf_builder.rejection_unsupported_claim reason=%s ent=%r key=%r val=%r chunk_id=%s",
                    reason,
                    ent_name,
                    prop_key,
                    prop_val,
                    chunk_id,
                )
                rejected_count += 1
                continue

            # Assertion passes all grounding checks!
            norm_name = normalize_entity_name(ent_name)
            ent_id = compute_entity_id(workspace_id, document_id, norm_name)
            if ent_id not in valid_entities_map:
                valid_entities_map[ent_id] = OkfEntityRecord(
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    normalized_name=norm_name,
                    entity_type="organization",
                    document_id=document_id,
                    document_version=document_version,
                    created_at=timestamp,
                )

            ass_id = compute_assertion_id(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entity_id=ent_id,
                property_key=prop_key,
                property_value=prop_val,
                source_chunk_id=str(chunk_id),
            )

            valid_assertions.append(
                OkfPropertyAssertion(
                    assertion_id=ass_id,
                    workspace_id=workspace_id,
                    entity_id=ent_id,
                    entity_name=ent_name,
                    property_key=prop_key,
                    property_value=prop_val,
                    value_type=val_type if val_type in ("string", "number", "date", "boolean", "schema_type") else "string",
                    source_chunk_id=str(chunk_id),
                    document_id=document_id,
                    document_version=document_version,
                    page_number=page_num,
                    table_name=None,
                    column_name=None,
                    confidence_score=1.0,
                    validation_status="verified",
                    created_at=timestamp,
                )
            )

        manifest = OkfManifest(
            workspace_id=workspace_id,
            document_id=document_id,
            document_version=document_version,
            entities=tuple(valid_entities_map.values()),
            assertions=tuple(valid_assertions),
            rejected_assertions_count=rejected_count,
            status="ready" if valid_assertions else ("failed" if (budget.calls_made > 0 or budget.exhausted) and not raw_candidates else "empty"),
            extraction_tokens=budget.input_tokens_used + budget.output_tokens_used,
            model_calls=budget.calls_made,
            idempotency_key=idempotency_key,
            created_at=timestamp,
        )

        try:
            self.repo.save_manifest(workspace_id, manifest)
        except Exception as save_err:
            logger.warning(
                "okf_builder.final_manifest_save_failed ws=%s doc=%s: %s (failing open per ADR-001)",
                workspace_id,
                document_id,
                save_err,
            )
            manifest = OkfManifest(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version=document_version,
                entities=(),
                assertions=(),
                rejected_assertions_count=rejected_count,
                status="failed",
                extraction_tokens=manifest.extraction_tokens,
                model_calls=manifest.model_calls,
                idempotency_key=idempotency_key,
                created_at=timestamp,
            )

        logger.info(
            "okf_builder.pdf_ready ws=%s doc=%s ver=%s assertions=%d rejected=%d tokens=%d calls=%d status=%s",
            workspace_id,
            document_id,
            document_version,
            len(manifest.assertions),
            rejected_count,
            manifest.extraction_tokens,
            budget.calls_made,
            manifest.status,
        )
        return manifest

    def _invoke_bedrock_extraction(
        self,
        client: Any,
        chunks: Sequence[Any],
        workspace_id: str,
        document_id: str,
        budget: DocumentExtractionBudget | None = None,
    ) -> tuple[str, dict[str, int]]:
        """Execute Bedrock Converse API with token caps, 10s deadline, and max 1 retry on throttling."""
        if budget is None:
            budget = DocumentExtractionBudget(
                workspace_id=workspace_id,
                document_id=document_id,
                document_version="v_default",
                max_elapsed_s=10.0,
            )

        system_prompt = (
            "You are a factual property extraction system for an enterprise knowledge retrieval engine.\n"
            "Extract entities and factual property assertions from the provided document chunks.\n"
            "Return ONLY valid JSON matching this schema:\n"
            "[\n"
            "  {\n"
            '    "entity_name": "Entity Name",\n'
            '    "property_key": "property_name",\n'
            '    "property_value": "exact verbatim text from chunk",\n'
            '    "source_chunk_id": "chunk id from header"\n'
            "  }\n"
            "]\n"
            "CRITICAL RULES:\n"
            "1. 'property_value' MUST appear VERBATIM in the chunk text.\n"
            "2. 'source_chunk_id' MUST match the bracketed chunk id exactly.\n"
            "3. Do not invent facts, extrapolate, or hallucinate claims."
        )

        sys_prompt_bytes = len(system_prompt.encode("utf-8")) + 32
        max_prompt_bytes = max(100, 2000 - sys_prompt_bytes)
        user_content_blocks = []
        for ch in chunks:
            ch_id = str(getattr(ch, "id", ""))
            ch_text = getattr(ch, "text", "") or ""
            if len(ch_text.encode("utf-8")) > max_prompt_bytes:
                ch_text = ch_text.encode("utf-8")[:max_prompt_bytes].decode("utf-8", errors="ignore")
            user_content_blocks.append(f"<source_chunk id=\"{ch_id}\">\n{ch_text}\n</source_chunk>")

        model_id = getattr(settings, "OKF_BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
        if model_id not in SUPPORTED_OKF_MODELS:
            budget.exhausted = True
            budget.exhaustion_reason = f"unsupported_model_configuration ({model_id})"
            logger.error(
                "okf_builder.unsupported_model model_id=%s not in supported configuration %s",
                model_id,
                SUPPORTED_OKF_MODELS,
            )
            return "", {"input_tokens": 0, "output_tokens": 0}

        user_prompt = "\n\n".join(user_content_blocks)
        estimated_prompt_tokens = count_request_tokens(system_prompt, user_prompt, model_id=model_id)
        max_output_tokens = 600

        # Test environment fallback
        if client is None:
            if not budget.reserve_call(estimated_prompt_tokens, max_output_tokens):
                logger.warning(
                    "okf_builder.test_mode_budget_exhausted calls=%d reason=%s",
                    budget.calls_made,
                    budget.exhaustion_reason,
                )
                return "", {"input_tokens": 0, "output_tokens": 0}
            out_json, stats = self._test_mode_fallback(chunks)
            budget.reconcile_call(estimated_prompt_tokens, max_output_tokens, stats["input_tokens"], stats["output_tokens"])
            return out_json, stats

        messages = [{"role": "user", "content": [{"text": user_prompt}]}]
        max_attempts = 2  # at most 1 retry on throttling (HTTP 429)

        for attempt in range(max_attempts):
            remaining_deadline = budget.max_elapsed_s - (time.perf_counter() - budget.started_at)
            if remaining_deadline <= 0:
                budget.exhausted = True
                budget.exhaustion_reason = f"elapsed_time_limit_reached ({budget.max_elapsed_s:.1f}s)"
                logger.warning(
                    "okf_builder.call_budget_exceeded attempt=%d reason=%s",
                    attempt, budget.exhaustion_reason,
                )
                return "", {"input_tokens": 0, "output_tokens": 0}

            if not budget.reserve_call(estimated_prompt_tokens, max_output_tokens):
                logger.warning(
                    "okf_builder.call_budget_exceeded attempt=%d reason=%s",
                    attempt, budget.exhaustion_reason,
                )
                return "", {"input_tokens": 0, "output_tokens": 0}

            # Persist in-flight attempt reservation to durable repository before provider call
            # This ensures that if the process crashes or dies mid-call, the durable store records
            # the attempted model call and does not silently reset upon restart/replay.
            # If persistence fails, do not call the provider; report failed optional enrichment
            # and preserve baseline availability per ADR-001.
            try:
                ver = getattr(budget, "document_version", "v1")
                ikey = getattr(budget, "idempotency_key", "") or f"{workspace_id}:{document_id}:{ver}"
                self.repo.save_manifest(
                    workspace_id,
                    OkfManifest(
                        workspace_id=workspace_id,
                        document_id=document_id,
                        document_version=ver,
                        entities=(),
                        assertions=(),
                        rejected_assertions_count=0,
                        status="failed",
                        extraction_tokens=budget.input_tokens_used + budget.output_tokens_used,
                        model_calls=budget.calls_made,
                        idempotency_key=ikey,
                        created_at=_now_utc(),
                    ),
                )
            except Exception as save_err:
                logger.error("okf_builder.reservation_persistence_failed: %s", save_err)
                budget.exhausted = True
                budget.exhaustion_reason = f"reservation_persistence_failed ({save_err})"
                return "", {"input_tokens": 0, "output_tokens": 0}

            call_timeout = max(0.001, budget.max_elapsed_s - (time.perf_counter() - budget.started_at))
            try:
                resp, status = self.provider_controller.run_provider_call(
                    client.converse,
                    timeout_s=call_timeout,
                    modelId=model_id,
                    messages=messages,
                    system=[{"text": system_prompt}],
                    inferenceConfig={
                        "temperature": 0.0,
                        "maxTokens": max_output_tokens,
                    },
                )

                if status == "caller_timeout":
                    budget.exhausted = True
                    elapsed_total = time.perf_counter() - budget.started_at
                    budget.exhaustion_reason = f"document_deadline_exceeded ({elapsed_total:.1f}s/{budget.max_elapsed_s:.1f}s)"
                    logger.warning("Bedrock OKF call exceeded document deadline: %s", budget.exhaustion_reason)
                    # Zero retries on timeouts per AC-3
                    return "", {"input_tokens": 0, "output_tokens": 0}

                if status == "admission_rejected":
                    budget.exhausted = True
                    budget.exhaustion_reason = "provider_concurrency_queue_saturated"
                    logger.warning("Bedrock OKF call rejected: %s", budget.exhaustion_reason)
                    return "", {"input_tokens": 0, "output_tokens": 0}

                output_text = resp["output"]["message"]["content"][0]["text"]
                raw_usage = resp.get("usage", {})
                usage = {
                    "input_tokens": raw_usage.get("inputTokens", estimated_prompt_tokens),
                    "output_tokens": raw_usage.get("outputTokens", 0),
                }
                budget.reconcile_call(estimated_prompt_tokens, max_output_tokens, usage["input_tokens"], usage["output_tokens"])
                return output_text, usage

            except Exception as e:
                err_str = str(e)
                if ("ThrottlingException" in err_str or "429" in err_str) and attempt < max_attempts - 1:
                    elapsed_so_far = time.perf_counter() - budget.started_at
                    retry_delay = 2.0
                    if elapsed_so_far + retry_delay < budget.max_elapsed_s:
                        logger.warning("Bedrock OKF throttled (429), retrying once after 2.0s...")
                        time.sleep(retry_delay)
                        continue
                    else:
                        budget.exhausted = True
                        budget.exhaustion_reason = f"retry_would_exceed_deadline ({elapsed_so_far + retry_delay:.1f}s/{budget.max_elapsed_s:.1f}s)"
                        logger.warning("Bedrock OKF throttled (429), cannot retry within deadline: %s", budget.exhaustion_reason)
                        return "", {"input_tokens": 0, "output_tokens": 0}

                # Zero retries on model errors, bad requests, or timeouts (AC-3)
                logger.warning("Bedrock OKF call failed (no retries per AC-3): %s", e)
                return "", {"input_tokens": 0, "output_tokens": 0}

        return "", {"input_tokens": 0, "output_tokens": 0}

    def _test_mode_fallback(self, chunks: Sequence[Any]) -> tuple[str, dict[str, int]]:
        """Deterministic extractor for offline unit test execution without AWS Bedrock."""
        items: list[dict[str, Any]] = []
        for ch in chunks:
            ch_id = str(getattr(ch, "id", ""))
            ch_text = getattr(ch, "text", "") or ""

            # Check for revenue patterns
            rev_match = re.search(r"(\$?\d+[\d,.]*\s*(?:billion|million|M|B)?)\s*(?:in\s+)?revenue", ch_text, re.IGNORECASE)
            if rev_match:
                val = rev_match.group(1).strip()
                # Find preceding capitalized words as entity name
                ent_match = re.search(r"([A-Z][a-zA-Z0-9\s]+?)\s+(?:reported|generated|recorded|had)", ch_text)
                ent_name = ent_match.group(1).strip() if ent_match else "Organization"
                items.append({
                    "entity_name": ent_name,
                    "property_key": "revenue",
                    "property_value": val,
                    "source_chunk_id": ch_id,
                })

            # Check for executive role patterns (e.g. John Smith is CEO)
            ceo_match = re.search(r"([A-Z][a-z]+\s+[A-Z][a-z]+)\s+(?:is|serves as|was appointed)\s+(?:the\s+)?(CEO|Chief Executive Officer|Author|President)", ch_text, re.IGNORECASE)
            if ceo_match:
                ent_name = ceo_match.group(1).strip()
                role_val = ceo_match.group(2).strip()
                items.append({
                    "entity_name": ent_name,
                    "property_key": "title",
                    "property_value": role_val,
                    "source_chunk_id": ch_id,
                })

        return json.dumps(items), {"input_tokens": 100, "output_tokens": len(items) * 20}

    def _parse_model_response(self, text: str) -> list[dict[str, Any]]:
        """Parse JSON array of entities and assertions from model output."""
        try:
            cleaned = text.strip()
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
            if match:
                cleaned = match.group(1).strip()
            else:
                bracket_start = cleaned.find("[")
                brace_start = cleaned.find("{")
                if bracket_start != -1 and (brace_start == -1 or bracket_start < brace_start):
                    bracket_end = cleaned.rfind("]")
                    if bracket_end > bracket_start:
                        cleaned = cleaned[bracket_start : bracket_end + 1]
                elif brace_start != -1:
                    brace_end = cleaned.rfind("}")
                    if brace_end > brace_start:
                        cleaned = cleaned[brace_start : brace_end + 1]

            data = json.loads(cleaned.strip())
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            elif isinstance(data, dict):
                if "facts" in data and isinstance(data["facts"], list):
                    return [d for d in data["facts"] if isinstance(d, dict)]
                return [data]
            return []
        except Exception as exc:
            logger.debug("Failed to parse OKF model JSON: %s (text=%r)", exc, text[:200])
            return []


# Module level singleton and legacy build_okf adapter
_builder_instance = OkfBuilder()


def extract_and_persist_okf(
    workspace_id: str,
    document_id: str,
    document_version: str,
    format: str,
    chunks: Sequence[Any],
    table_name: str | None = None,
    table_store: Any | None = None,
    repo: OkfRepositoryBase | None = None,
) -> OkfManifest:
    """Public helper for invoking OKF extraction and persistence."""
    builder = OkfBuilder(repo=repo) if repo else _builder_instance
    return builder.extract_and_persist(
        workspace_id=workspace_id,
        document_id=document_id,
        document_version=document_version,
        format=format,
        chunks=chunks,
        table_name=table_name,
        table_store=table_store,
    )


def _extract_tier3_smart(chunks: Any) -> tuple[list[dict], dict]:
    """Legacy helper stub for smart extraction tests."""
    return [], {"input_tokens": 0, "output_tokens": 0, "calls": 0}


def build_okf(document: Any) -> None:
    """Legacy backward-compatible entry point for build_okf."""
    doc_id = str(getattr(document, "id", ""))
    ws_id = getattr(document, "workspace_id", "default")
    ver = getattr(document, "version", doc_id)
    fmt = getattr(document, "source_format", "pdf")
    chunks = list(getattr(document, "chunks", ()))

    # Legacy mock / direct repository support (e.g. test_phase3)
    try:
        from db.database import CloudRepository
        repo = CloudRepository()
    except Exception:
        repo = None

    if hasattr(repo, "okf_properties_table") or hasattr(repo, "table"):
        try:
            tier3_props, token_stats = _extract_tier3_smart(chunks)
            all_props = list(tier3_props)
            try:
                from ingestion.concept_service import (
                    extract_tier1_patterns,
                    gate_and_rank_chunks,
                )
                gated = gate_and_rank_chunks(chunks)
                gated_chunks = [c for c, _ in gated]
                tier1_props = extract_tier1_patterns(gated_chunks)
                all_props = tier1_props + tier3_props
            except Exception:
                pass

            canonical_map: dict[str, str] = {}
            if all_props:
                try:
                    from ingestion.normalize_service import cluster_entities
                    entity_names = list({p["concept"] for p in all_props if p.get("concept")})
                    canonical_map = cluster_entities(entity_names)
                except Exception:
                    canonical_map = {}

            if hasattr(repo, "okf_properties_table"):
                for prop in all_props:
                    raw_name = prop.get("concept", "")
                    if not raw_name:
                        continue
                    canon = canonical_map.get(raw_name, raw_name)
                    concept_id = canon.strip().upper().replace(" ", "_")
                    prop_name = str(prop.get("property_name", ""))
                    chunk_id = str(prop.get("source_chunk_id", ""))
                    if not prop_name or not chunk_id:
                        continue
                    try:
                        from decimal import Decimal
                        repo.okf_properties_table.put_item(
                            Item={
                                "PK": f"ENTITY#{concept_id}",
                                "SK": f"PROP#{prop_name}#{chunk_id}",
                                "property_name": prop_name,
                                "property_value": str(prop.get("property_value", "")),
                                "source_chunk_id": chunk_id,
                                "confidence": Decimal(str(round(float(prop.get("confidence", 1.0)), 4))),
                                "doc_id": doc_id,
                            }
                        )
                    except Exception as err:
                        logger.debug("Legacy write property failed: %s", err)

            if hasattr(repo, "table") and token_stats.get("calls", 0) > 0:
                try:
                    from decimal import Decimal
                    repo.table.update_item(
                        Key={"PK": f"DOC#{doc_id}", "SK": "OKF_USAGE"},
                        UpdateExpression=(
                            "ADD input_tokens :i, output_tokens :o, extraction_calls :c "
                            "SET updated_at = :t"
                        ),
                        ExpressionAttributeValues={
                            ":i": Decimal(str(token_stats["input_tokens"])),
                            ":o": Decimal(str(token_stats["output_tokens"])),
                            ":c": Decimal(str(token_stats["calls"])),
                            ":t": str(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                        },
                    )
                except Exception as err:
                    logger.debug("Legacy write token usage failed: %s", err)
        except Exception as exc:
            logger.debug("Legacy okf_builder execution error: %s", exc)

    # Modern extraction and persistence
    try:
        extract_and_persist_okf(
            workspace_id=ws_id,
            document_id=doc_id,
            document_version=ver,
            format=fmt,
            chunks=chunks,
        )
    except Exception as exc:
        logger.warning("Legacy build_okf failed safely: %s", exc)
