"""Financial table metric, entity, period, and unit binding (0004).

Enforces:
  1. Complete verification of primary and required financial claims.
  2. Preservation of signs (minus signs and accounting parentheses).
  3. Exact decimal arithmetic for explicit units and scale conversions.
  4. Complete period evidence and exact date/duration comparisons.
  5. Distinction between monetary amounts and percentages.
  6. Scoring and prioritization of metric-aligned tables during evidence selection.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

MONTH_MAP: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

SCALE_MULTIPLIERS: dict[str, Decimal] = {
    "billions": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
    "millions": Decimal("1000000"),
    "million": Decimal("1000000"),
    "thousands": Decimal("1000"),
    "thousand": Decimal("1000"),
    "dollars": Decimal("1"),
    "dollar": Decimal("1"),
}


@dataclass(frozen=True)
class FinancialQueryBinding:
    """Binding of financial table query requirements."""

    metric: str | None
    entity: str | None
    duration_norm: str | None
    date_norm: str | None
    date_parsed: tuple[int | None, int | None, int | None] | None
    units: str | None
    is_percentage: bool = False
    is_active: bool = False
    metrics: tuple[str, ...] = ()
    target_requirements: tuple[tuple[str, str | None], ...] = ()


ENTITY_NAMES = ["Services", "Products", "iPhone", "iPad", "Mac", "Wearables"]


def classify_metric(text: str | None) -> str | None:
    """Classifies financial metric with strict precedence for specific phrases."""
    if not text:
        return None
    t_lower = text.lower()
    if re.search(r"\bgross\s+margin\b|\bgross\s+profit\b", t_lower):
        return "gross_margin"
    if re.search(r"\bcost\s+of\s+sales\b|\bcost\s+of\s+revenue\b|\bcogs\b", t_lower):
        return "cost_of_sales"
    if re.search(r"\brestructuring\s+costs?\b|\brestructuring\b", t_lower):
        return "restructuring_costs"
    if re.search(r"\boperating\s+income\b|\boperating\s+profit\b|\boperating\s+loss\b", t_lower):
        return "operating_income"
    if re.search(r"\bnet\s+income\b|\bnet\s+loss\b|\bnet\s+earnings\b", t_lower):
        return "net_income"
    if re.search(r"\b(?:net\s+)?sales\b|\b(?:net\s+)?revenue\b", t_lower):
        return "net_sales"
    return None


def extract_entity_from_text(text: str | None) -> str | None:
    """Extracts known business entity or product segment from text."""
    if not text:
        return None
    for e in ENTITY_NAMES:
        if re.search(r"\b" + e + r"\b", text, re.I):
            return e
    if re.search(r"\brestructuring\b", text, re.I):
        return "Restructuring costs"
    return None


def parse_query_requirements(query: str) -> list[tuple[str, str | None]]:
    """Deterministically extracts all (metric, entity) pairs requested by the query."""
    if not query:
        return []
    q_lower = query.lower()
    clauses = [c.strip() for c in re.split(r";|\band\b|,", q_lower) if c.strip()]
    reqs: list[tuple[str, str | None]] = []
    pending_entities: list[str] = []
    last_entity: str | None = None

    for c in clauses:
        m = classify_metric(c)
        e = extract_entity_from_text(c)
        is_total = bool(re.search(r"\btotal\b|\boverall\b", c, re.I))
        if m and e:
            last_entity = e
            if pending_entities:
                for pe in pending_entities:
                    reqs.append((m, pe))
                pending_entities = []
            reqs.append((m, e))
        elif m and not e:
            eff_e = None if is_total else last_entity
            if pending_entities:
                for pe in pending_entities:
                    reqs.append((m, pe))
                pending_entities = []
            reqs.append((m, eff_e))
        elif e and not m:
            pending_entities.append(e)
            last_entity = e

    if pending_entities:
        all_m = [r[0] for r in reqs if r[0]]
        def_m = all_m[0] if all_m else classify_metric(query)
        if def_m:
            for pe in pending_entities:
                reqs.append((def_m, pe))

    # Deduplicate while preserving order
    seen: set[tuple[str, str | None]] = set()
    unique_reqs: list[tuple[str, str | None]] = []
    for r in reqs:
        if r not in seen and r[0] is not None:
            seen.add(r)
            unique_reqs.append(r)

    if not unique_reqs:
        m = classify_metric(query)
        e = extract_entity_from_text(query)
        if m:
            unique_reqs.append((m, e))

    return unique_reqs


def parse_date_components(text: str | None) -> tuple[int | None, int | None, int | None] | None:
    """Extracts (year, month, day) as exact integers from text."""
    if not text:
        return None
    t_lower = text.lower()
    m_full = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s*(\d{1,2})\s*,?\s*(\d{4})\b",
        t_lower,
    )
    if m_full:
        m_name, d_str, y_str = m_full.groups()
        return int(y_str), MONTH_MAP[m_name], int(d_str)

    m_my = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{4})\b",
        t_lower,
    )
    if m_my:
        m_name, y_str = m_my.groups()
        return int(y_str), MONTH_MAP[m_name], None

    m_yr = re.search(r"\b(20\d{2}|19\d{2})\b", t_lower)
    if m_yr:
        return int(m_yr.group(1)), None, None

    return None


def normalize_duration(text: str | None) -> str | None:
    """Normalizes supported duration expressions to canonical categories."""
    if not text:
        return None
    t_lower = text.lower()
    if re.search(r"\b(?:three|3)\s*[- ]\s*months?\b|\bfirst\s+quarter\b|\bsecond\s+quarter\b|\bthird\s+quarter\b|\bfourth\s+quarter\b|\bquarter\b|\bq[1-4]\b", t_lower):
        return "three_months"
    if re.search(r"\b(?:nine|9)\s*[- ]\s*months?\b", t_lower):
        return "nine_months"
    if re.search(r"\b(?:six|6)\s*[- ]\s*months?\b", t_lower):
        return "six_months"
    if re.search(r"\b(?:twelve|12)\s*[- ]\s*months?\b|\byear\s+ended\b|\bfull\s+year\b|\bannual\b", t_lower):
        return "twelve_months"
    return None


def detect_table_scale(chunk_text: str) -> Decimal:
    """Determines table unit multiplier: 10^6 for millions, 10^9 for billions, etc."""
    if not chunk_text:
        return Decimal("1")
    t_lower = chunk_text.lower()
    if re.search(r"\b(?:dollars\s+)?in\s+billions\b", t_lower):
        return Decimal("1000000000")
    if re.search(r"\b(?:dollars\s+)?in\s+millions\b", t_lower):
        return Decimal("1000000")
    if re.search(r"\b(?:dollars\s+)?in\s+thousands\b", t_lower):
        return Decimal("1000")
    return Decimal("1")


def bind_financial_query(query: str) -> FinancialQueryBinding:
    """Extracts and binds target metric, entity, period, and units from query grammar."""
    if not query:
        return FinancialQueryBinding(None, None, None, None, None, None, False, False, (), ())

    q_lower = query.lower()

    # 1. Target metric and entity requirement classification
    req_pairs = parse_query_requirements(query)
    primary_metric = req_pairs[0][0] if req_pairs else classify_metric(query)
    primary_entity = req_pairs[0][1] if req_pairs else extract_entity_from_text(query)
    all_metrics = tuple(r[0] for r in req_pairs if r[0])

    # 2. Period duration classification
    duration_norm = normalize_duration(q_lower)

    # 3. Period date extraction
    date_parsed = parse_date_components(q_lower)
    date_norm = None
    if date_parsed:
        y, m, d = date_parsed
        parts = []
        if m:
            parts.append(str(m))
        if d:
            parts.append(str(d))
        if y:
            parts.append(str(y))
        date_norm = "-".join(parts)

    # 4. Units extraction
    units = None
    is_pct = False
    if "percent" in q_lower or "%" in q_lower:
        units = "percentage"
        is_pct = True
    elif "billion" in q_lower:
        units = "billions"
    elif "million" in q_lower:
        units = "millions"
    elif "thousand" in q_lower:
        units = "thousands"
    elif "$" in q_lower or "dollar" in q_lower:
        units = "dollars"

    has_any_entity = bool(primary_entity or any(r_e for _, r_e in req_pairs))
    is_active = bool(all_metrics and (has_any_entity or duration_norm or date_parsed or units))

    return FinancialQueryBinding(
        metric=primary_metric,
        entity=primary_entity,
        duration_norm=duration_norm,
        date_norm=date_norm,
        date_parsed=date_parsed,
        units=units,
        is_percentage=is_pct,
        is_active=is_active,
        metrics=all_metrics,
        target_requirements=tuple(req_pairs),
    )


def parse_financial_table_cells(text: str) -> list[dict[str, Any]]:
    """Deterministically extracts cells, headers, section categories, signs, and period columns from tabular text."""
    if not text:
        return []

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    durations: list[str] = []
    dates: list[str] = []
    col_headers: list[dict[str, str | None]] = []
    current_section = None
    cells: list[dict[str, Any]] = []
    table_scale = detect_table_scale(text)

    top_header = None
    if lines and len(lines[0]) < 60 and not re.search(r"\d", lines[0]):
        top_header = lines[0].lower()

    dur_re = re.compile(r"\b(three|six|nine|twelve|\d+)\s+months\s+ended|\byear\s+ended\b", re.I)
    date_re = re.compile(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\s*,\s*\d{4}\b",
        re.I,
    )
    # Pattern matching signed/negative accounting numbers: (1,250), -$1,250, 1,250, 40.1%
    token_pattern = re.compile(
        r"\(\s*\$?\s*[\d,]+(?:\.\d+)?\s*%?\s*\)|[-+]?\$?\s*[\d,]+(?:\.\d+)?%?"
    )

    for line in lines:
        dur_matches = list(dur_re.finditer(line))
        date_matches = list(date_re.finditer(line))

        if dur_matches:
            durations = [m.group(0).lower() for m in dur_matches]
        if date_matches:
            dates = [m.group(0).lower() for m in date_matches]
            if durations and len(dates) % len(durations) == 0:
                dates_per_dur = len(dates) // len(durations)
                col_headers = []
                for d_idx, dur in enumerate(durations):
                    for d in dates[d_idx * dates_per_dur : (d_idx + 1) * dates_per_dur]:
                        col_headers.append({"duration": dur, "date": d})
            elif len(durations) == 1:
                col_headers = [{"duration": durations[0], "date": d} for d in dates]
            elif len(durations) > 1:
                col_headers = [{"duration": durations[i] if i < len(durations) else durations[-1], "date": d} for i, d in enumerate(dates)]
            else:
                col_headers = [{"duration": None, "date": d} for d in dates]
            continue
        if dur_matches and not date_matches:
            continue

        if line.endswith(":") and not re.search(r"\d", line):
            current_section = line[:-1].strip().lower()
            continue

        raw_matches = list(token_pattern.finditer(line))
        data_matches = []
        for m_idx, m in enumerate(raw_matches):
            tok_raw = m.group(0).strip()
            next_m = raw_matches[m_idx + 1] if m_idx + 1 < len(raw_matches) else None
            if re.fullmatch(r"\(\s*\d\s*\)", tok_raw) and next_m:
                continue
            between = line[m.end() : next_m.start()] if next_m else line[m.end():]
            if re.search(r"[a-zA-Z]{2,}", between):
                continue
            if not next_m and re.search(r"[a-zA-Z]{2,}", line[m.end():]):
                continue
            data_matches.append(m)

        parsed_tokens = []
        for m in data_matches:
            tok = m.group(0)
            tok_clean = tok.strip()
            if not tok_clean or not re.search(r"\d", tok_clean):
                continue
            is_neg = False
            if tok_clean.startswith("(") and tok_clean.endswith(")"):
                is_neg = True
                tok_clean = tok_clean[1:-1].strip()
            elif tok_clean.startswith("-"):
                is_neg = True
                tok_clean = tok_clean[1:].strip()
            is_pct = "%" in tok_clean
            tok_clean = tok_clean.replace("$", "").replace("%", "").replace(",", "").strip()
            try:
                d_val = Decimal(tok_clean)
                if is_neg:
                    d_val = -d_val
                parsed_tokens.append((d_val, is_neg, is_pct, tok, m.start()))
            except Exception:
                continue

        if parsed_tokens:
            idx_tok = parsed_tokens[0][4]
            row_label = line[:idx_tok].replace("$", "").strip()
            if row_label:
                effective_section = current_section or top_header
                for c_idx, (d_val, is_neg, is_pct, raw_tok, _) in enumerate(parsed_tokens):
                    col_info = col_headers[c_idx] if c_idx < len(col_headers) else {}
                    cells.append({
                        "section": effective_section,
                        "row": row_label,
                        "value": d_val,
                        "sign": -1 if is_neg else 1,
                        "is_percentage": is_pct,
                        "col_idx": c_idx,
                        "duration": col_info.get("duration"),
                        "duration_norm": normalize_duration(col_info.get("duration")),
                        "date": col_info.get("date"),
                        "date_parsed": parse_date_components(col_info.get("date")),
                        "scale": table_scale,
                        "raw_line": line,
                        "chunk_text": text,
                    })

    return cells


def extract_financial_claims(text: str) -> list[dict[str, Any]]:
    """Extracts structured financial claims from an answer, distinguishing primary from comparison claims."""
    if not text:
        return []

    # Mask period dates and durations so date numbers do not get treated as financial quantities
    period_pattern = re.compile(
        r"\b(?:january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\s+\d{1,2}(?:\s*,\s*\d{4})?\b"
        r"|\b(?:three|six|nine|twelve|\d+)\s+months?\b"
        r"|\b20\d{2}\b",
        re.I,
    )
    masked_text = period_pattern.sub(" [PERIOD] ", text)

    clauses = re.split(
        r";|\band\b|\bcompared\s+to\b|\brather\s+than\b|\bversus\b|\bvs\.?\b|\bup\s+from\b|\bdown\s+from\b|\bincrease\s+from\b|\bdecrease\s+from\b|[\r\n]+|\.(?=\s+[A-Z0-9]|\s*$)|(?<=\s)[•\-\*]\s+",
        masked_text,
        flags=re.I,
    )
    claims: list[dict[str, Any]] = []

    token_pattern = re.compile(
        r"(\(\s*[^\d)]*\$?\s*[\d,]+(?:\.\d+)?\s*%?\s*\)|[-+]?\$?\s*[\d,]+(?:\.\d+)?%?)\s*(billions?|millions?|thousands?|dollars?)?",
        re.I,
    )

    comp_keywords = re.compile(
        r"\bcompared\b|\brather\b|\bversus\b|\bvs\b|\bprior\b|\bprevious\b|\bup\s+from\b|\bdown\s+from\b|\bincrease\s+from\b|\bdecrease\s+from\b",
        re.I,
    )

    last_pos = 0
    for idx, clause in enumerate(clauses):
        c_stripped = clause.strip()
        if not c_stripped:
            continue
        c_pos = masked_text.find(clause, last_pos)
        separator = masked_text[last_pos:c_pos] if c_pos != -1 else ""
        if c_pos != -1:
            last_pos = c_pos + len(clause)

        is_comp = False
        if idx > 0:
            if comp_keywords.search(separator) or comp_keywords.search(clause[:30]):
                is_comp = True
        for m in token_pattern.finditer(clause):
            raw = m.group(1).strip()
            unit_str = m.group(2)
            if not re.search(r"\d", raw):
                continue
            is_neg = False
            c_val = raw
            if c_val.startswith("(") and c_val.endswith(")"):
                is_neg = True
                c_val = c_val[1:-1].strip()
            elif c_val.startswith("-"):
                is_neg = True
                c_val = c_val[1:].strip()
            elif c_val.startswith("$-") or c_val.startswith("-$"):
                is_neg = True
                c_val = c_val.replace("$-", "").replace("-$", "").strip()

            is_pct = "%" in c_val
            c_val = c_val.replace("$", "").replace("%", "").replace(",", "").strip()
            try:
                dec_val = Decimal(c_val)
                if is_neg:
                    dec_val = -dec_val
            except Exception:
                continue

            unit = None
            if unit_str:
                u_low = unit_str.lower()
                if "billion" in u_low:
                    unit = "billions"
                elif "million" in u_low:
                    unit = "millions"
                elif "thousand" in u_low:
                    unit = "thousands"
                elif "dollar" in u_low:
                    unit = "dollars"

            ent = extract_entity_from_text(clause)
            met = classify_metric(clause)

            claims.append({
                "value": dec_val,
                "sign": -1 if is_neg else 1,
                "unit": unit,
                "is_percentage": is_pct,
                "entity": ent,
                "metric": met,
                "is_comparison": is_comp,
                "raw": raw,
                "clause": clause.strip(),
            })

    return claims


def verify_financial_cell_binding(
    chunk_or_chunks: str | list[str] | list[Any],
    answer_value: str,
    binding: FinancialQueryBinding,
) -> tuple[bool, str]:
    """Verifies that every required financial claim in the answer is verified by an aligned cell.

    Checks:
      1. Complete period evidence: required date/duration must be present in cited table metadata.
      2. Exact normalized date comparison: year, month, and day must match exactly (June 27 fails June 7).
      3. Metric and entity alignment: target metric and segment entity must align.
      4. Sign preservation: positive answers fail negative cells, negative answers fail positive cells.
      5. Explicit units and scale arithmetic: exact arithmetic with Decimal scale multipliers.
      6. Separation of percentages from monetary amounts.
      7. Rejection of unsupported primary answers even if secondary comparison numbers exist.
      8. Multi-chunk support: verifies claims across independently parsed cited chunks.
    """
    if not binding.is_active:
        return True, "inactive_binding"

    if not chunk_or_chunks:
        return False, "empty_chunk_text"

    cells: list[dict[str, Any]] = []
    if isinstance(chunk_or_chunks, str):
        cells = parse_financial_table_cells(chunk_or_chunks)
    elif isinstance(chunk_or_chunks, list):
        for item in chunk_or_chunks:
            if isinstance(item, str):
                c_text = item
                cid = None
            elif isinstance(item, dict) and "value" in item and "sign" in item:
                cells.append(item)
                continue
            else:
                c_text = getattr(item, "text", "")
                cid = getattr(item, "id", None)
            if c_text:
                parsed = parse_financial_table_cells(c_text)
                if cid:
                    for c in parsed:
                        c["chunk_id"] = str(cid)
                cells.extend(parsed)
    else:
        c_text = getattr(chunk_or_chunks, "text", "")
        if c_text:
            cells = parse_financial_table_cells(c_text)

    if not cells:
        return False, "no_cells_found_in_chunk"

    # 1. Require complete period evidence
    if binding.duration_norm is not None:
        has_duration_meta = any(c.get("duration_norm") is not None for c in cells)
        if not has_duration_meta:
            return False, "missing_period_duration_in_table"

    if binding.date_parsed is not None:
        has_date_meta = any(c.get("date_parsed") is not None for c in cells)
        if not has_date_meta:
            return False, "missing_period_date_in_table"

    # 2. Extract financial claims from answer
    claims = extract_financial_claims(str(answer_value))
    if not claims:
        # Fallback for concise numeric answer
        clean_raw = str(answer_value).strip()
        is_neg = clean_raw.startswith("-") or (clean_raw.startswith("(") and clean_raw.endswith(")"))
        is_pct = "%" in clean_raw
        clean_digits = re.sub(r"[^\d.]", "", clean_raw)
        if not clean_digits:
            return False, "no_numeric_answer"
        try:
            val_d = Decimal(clean_digits)
            if is_neg:
                val_d = -val_d
            claims = [{
                "value": val_d,
                "sign": -1 if is_neg else 1,
                "unit": None,
                "is_percentage": is_pct,
                "entity": binding.entity,
                "metric": binding.metric,
                "is_comparison": False,
                "raw": clean_raw,
                "clause": clean_raw,
            }]
        except Exception:
            return False, "invalid_numeric_format"

    # Filter to primary non-comparison claims
    primary_claims = [c for c in claims if not c.get("is_comparison")]
    if not primary_claims and claims:
        primary_claims = [claims[0]]

    req_pairs = list(binding.target_requirements) if getattr(binding, "target_requirements", None) else []
    if not req_pairs and binding.metric:
        req_pairs = [(binding.metric, binding.entity)]
    expected_metrics = set(binding.metrics) if getattr(binding, "metrics", None) else ({binding.metric} if binding.metric else set())

    satisfied_reqs: set[tuple[str | None, str | None]] = set()

    # 3. Verify EVERY primary claim against table cells
    for claim in primary_claims:
        claim_verified = False
        target_metric = claim.get("metric") or binding.metric

        # Resolve target entity for this claim
        if claim.get("entity"):
            target_entity = claim["entity"]
        else:
            # Look up expected entity for this metric in requirements
            cand_req_entities = [r_e for r_m, r_e in req_pairs if r_m == target_metric]
            if None in cand_req_entities:
                target_entity = None
            elif cand_req_entities:
                target_entity = cand_req_entities[0]
            else:
                target_entity = binding.entity

        if claim.get("metric") and expected_metrics and claim["metric"] not in expected_metrics:
            logger.warning(
                "verify_financial_cell_binding: claim metric %s contradicts expected metrics %s",
                claim["metric"],
                expected_metrics,
            )
            return False, "claim_metric_contradicts_binding"

        for cell in cells:
            sec = (cell.get("section") or "").lower()
            row = (cell.get("row") or "").lower()

            # A. Metric category check
            chunk_context = cell.get("chunk_text") or (chunk_or_chunks if isinstance(chunk_or_chunks, str) else "")
            c_text_lower = chunk_context.lower() if chunk_context else ""
            if target_metric == "net_sales":
                if "gross margin" in sec or "cost of sales" in sec or "operating expenses" in sec:
                    continue
                if not ("sales" in sec or "revenue" in sec or "operations" in c_text_lower):
                    continue
            elif target_metric == "gross_margin":
                if not ("gross margin" in sec or "gross profit" in sec or "gross margin" in row):
                    continue
            elif target_metric == "cost_of_sales":
                if not ("cost of sales" in sec or "cost of revenue" in sec or "cogs" in sec or "cost of sales" in row or "cost of revenue" in row):
                    continue
            elif target_metric == "restructuring_costs":
                if not ("restructuring" in row or "restructuring" in sec or "operating income" in sec):
                    continue
            elif target_metric == "operating_income":
                if not (
                    "operating income" in sec
                    or "operating profit" in sec
                    or "operating income" in row
                    or "operating profit" in row
                    or "operating loss" in row
                ):
                    continue
            elif target_metric == "net_income":
                if not (
                    "net income" in sec
                    or "net earnings" in sec
                    or "net income" in row
                    or "net earnings" in row
                    or "net loss" in row
                ):
                    continue

            # B. Entity check
            if target_entity:
                if target_entity.lower() not in row:
                    continue
            else:
                row_has_segment_entity = any(re.search(r"\b" + e + r"\b", row, re.I) for e in ENTITY_NAMES)
                if row_has_segment_entity and "total" not in row:
                    continue

            # C. Duration check
            if binding.duration_norm:
                cell_dur = cell.get("duration_norm")
                if cell_dur is None or cell_dur != binding.duration_norm:
                    continue

            # D. Date check (exact day, month, year)
            if binding.date_parsed:
                c_dt = cell.get("date_parsed")
                if c_dt is None:
                    continue
                req_y, req_m, req_d = binding.date_parsed
                cell_y, cell_m, cell_d = c_dt
                if req_y is not None and cell_y != req_y:
                    continue
                if req_m is not None and cell_m != req_m:
                    continue
                if req_d is not None and cell_d != req_d:
                    continue

            # E. Sign check
            if claim["sign"] != cell["sign"]:
                continue

            # F. Percentage versus monetary check
            if claim["is_percentage"] != cell["is_percentage"]:
                continue
            if binding.units == "dollars" and claim["is_percentage"]:
                continue

            # G. Scale and exact value arithmetic check
            c_val = cell["value"]
            q_val = claim["value"]
            if cell["is_percentage"]:
                if q_val != c_val:
                    continue
            else:
                table_scale = cell.get("scale", Decimal("1"))
                cell_abs = c_val * table_scale
                claim_unit = claim.get("unit")
                if claim_unit:
                    claim_multiplier = SCALE_MULTIPLIERS.get(claim_unit, Decimal("1"))
                    claim_abs = q_val * claim_multiplier
                    if claim_abs != cell_abs:
                        continue
                else:
                    # Raw numeric match without units stated
                    if q_val != c_val and q_val != cell_abs:
                        continue

            claim_verified = True
            for req in req_pairs:
                r_m, r_e = req
                if r_m == target_metric:
                    if r_e is None:
                        if target_entity is None or target_entity.lower() == "total":
                            satisfied_reqs.add(req)
                    elif target_entity and r_e.lower() == target_entity.lower():
                        satisfied_reqs.add(req)
            break

        if not claim_verified:
            logger.warning("verify_financial_cell_binding: claim=%s failed verification against table", claim)
            return False, "claim_verification_failed"

    # Require that every explicitly requested requirement was satisfied
    for req in req_pairs:
        if req not in satisfied_reqs:
            logger.warning("verify_financial_cell_binding: requested requirement %s was not satisfied", req)
            return False, "requested_metric_omitted_or_unsupported"

    return True, "verified"


def score_table_chunk_metric_affinity(chunk_text: str, binding: FinancialQueryBinding) -> float:
    """Computes an affinity adjustment score for table evidence selection based on bound requirements."""
    if not binding.is_active or not chunk_text:
        return 0.0

    text_lower = chunk_text.lower()
    score = 0.0

    req_metrics = set(binding.metrics) if getattr(binding, "metrics", None) else ({binding.metric} if binding.metric else set())

    if "net_sales" in req_metrics:
        if "condensed consolidated statements of operations" in text_lower or "net sales:" in text_lower:
            score += 2.0
        if "gross margin" in text_lower and "statements of operations" not in text_lower and "gross_margin" not in req_metrics:
            score -= 2.0

    if "gross_margin" in req_metrics:
        if "gross margin percentage" in text_lower or "products and services gross margin" in text_lower or text_lower.startswith("gross margin"):
            score += 3.0
        elif "gross margin" in text_lower:
            score += 1.0
        if "condensed consolidated statements of operations" in text_lower and "net_sales" not in req_metrics:
            score -= 1.0

    if "cost_of_sales" in req_metrics:
        if "cost of sales" in text_lower or "cost of revenue" in text_lower:
            score += 2.0

    if "restructuring_costs" in req_metrics:
        if "restructuring" in text_lower:
            score += 2.0

    if "operating_income" in req_metrics:
        if "operating income" in text_lower or "operating profit" in text_lower or "operating loss" in text_lower or "statements of operations" in text_lower:
            score += 2.0

    if "net_income" in req_metrics:
        if "net income" in text_lower or "net earnings" in text_lower or "statements of operations" in text_lower:
            score += 2.0

    target_entities = set()
    if binding.entity:
        target_entities.add(binding.entity)
    for _, r_e in getattr(binding, "target_requirements", ()):
        if r_e:
            target_entities.add(r_e)
    for ent in target_entities:
        if ent.lower() in text_lower:
            score += 0.5
            break

    return score
