"""Financial table metric, entity, period, and unit binding (0004).

Enforces:
  1. Binding query requirements (metric, entity, period, units) without question IDs or expected values.
  2. Scoring and prioritization of metric-aligned tables during evidence selection.
  3. Strict cell-level verification of cited financial table values before delivery.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FinancialQueryBinding:
    """Binding of financial table query requirements."""

    metric: str | None
    entity: str | None
    duration_norm: str | None
    date_norm: str | None
    units: str | None
    is_active: bool = False


def bind_financial_query(query: str) -> FinancialQueryBinding:
    """Extracts and binds target metric, entity, period, and units from query grammar."""
    if not query:
        return FinancialQueryBinding(None, None, None, None, None, False)

    q_lower = query.lower()

    # 1. Target metric classification
    metric = None
    if re.search(r"\bgross\s+margin\b|\bgross\s+profit\b", q_lower):
        metric = "gross_margin"
    elif re.search(r"\bcost\s+of\s+sales\b|\bcost\s+of\s+revenue\b|\bcogs\b", q_lower):
        metric = "cost_of_sales"
    elif re.search(r"\boperating\s+income\b|\boperating\s+profit\b|\boperating\s+loss\b", q_lower):
        metric = "operating_income"
    elif re.search(r"\bnet\s+income\b|\bnet\s+loss\b|\bnet\s+earnings\b", q_lower):
        metric = "net_income"
    elif re.search(r"\b(?:net\s+)?sales\b|\b(?:net\s+)?revenue\b", q_lower):
        metric = "net_sales"

    # 2. Entity / Segment extraction
    entity = None
    m_ent = re.search(r"\b(services|products|iphone|ipad|mac|wearables)\b", q_lower)
    if m_ent:
        entity = m_ent.group(1).capitalize()

    # 3. Period duration classification
    duration_norm = None
    if re.search(r"\bthree\s+months?\b|\b3\s+months?\b|\bfirst\s+quarter\b|\bsecond\s+quarter\b|\bthird\s+quarter\b|\bquarter\b", q_lower):
        duration_norm = "three_months"
    elif re.search(r"\bnine\s+months?\b|\b9\s+months?\b", q_lower):
        duration_norm = "nine_months"
    elif re.search(r"\bsix\s+months?\b|\b6\s+months?\b", q_lower):
        duration_norm = "six_months"
    elif re.search(r"\btwelve\s+months?\b|\byear\s+ended\b|\bfull\s+year\b|\bannual\b", q_lower):
        duration_norm = "twelve_months"

    # 4. Period date extraction
    date_norm = None
    date_m = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\s*,\s*(\d{4})\b",
        q_lower,
    )
    if date_m:
        date_norm = date_m.group(0).replace(" ", "")
    else:
        yr_m = re.search(r"\b(20\d{2})\b", q_lower)
        if yr_m:
            date_norm = yr_m.group(1)

    # 5. Units extraction
    units = None
    if "million" in q_lower:
        units = "millions"
    elif "billion" in q_lower:
        units = "billions"
    elif "$" in q_lower or "dollar" in q_lower:
        units = "dollars"

    is_active = bool(metric and (entity or duration_norm or date_norm or units))

    return FinancialQueryBinding(
        metric=metric,
        entity=entity,
        duration_norm=duration_norm,
        date_norm=date_norm,
        units=units,
        is_active=is_active,
    )


def parse_financial_table_cells(text: str) -> list[dict[str, Any]]:
    """Deterministically extracts cells, headers, section categories, and period columns from tabular text."""
    if not text:
        return []

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    durations: list[str] = []
    dates: list[str] = []
    dur_re = re.compile(r"\b(three|six|nine|twelve|\d+)\s+months\s+ended|\byear\s+ended\b", re.I)
    date_re = re.compile(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}\s*,\s*\d{4}\b",
        re.I,
    )
    col_headers: list[dict[str, str | None]] = []
    current_section = None
    cells: list[dict[str, Any]] = []

    # Detect top-level table header if present (e.g. "Gross Margin")
    top_header = None
    if lines and len(lines[0]) < 60 and not re.search(r"\d", lines[0]):
        top_header = lines[0].lower()

    for line in lines:
        dur_matches = list(dur_re.finditer(line))
        date_matches = list(date_re.finditer(line))

        if dur_matches:
            durations = [m.group(0).lower() for m in dur_matches]
        if date_matches:
            dates = [m.group(0).lower().replace(" ", "") for m in date_matches]
            if len(durations) == 2 and len(dates) == 4:
                col_headers = [
                    {"duration": durations[0], "date": dates[0]},
                    {"duration": durations[0], "date": dates[1]},
                    {"duration": durations[1], "date": dates[2]},
                    {"duration": durations[1], "date": dates[3]},
                ]
            elif len(durations) == 1 and len(dates) >= 1:
                col_headers = [
                    {"duration": durations[0], "date": d}
                    for d in dates
                ]
            else:
                col_headers = [
                    {"duration": durations[0] if durations else None, "date": d}
                    for d in dates
                ]
            continue
        if dur_matches and not date_matches:
            continue

        if line.endswith(":") and not re.search(r"\d", line):
            current_section = line[:-1].strip().lower()
            continue

        nums = re.findall(r"[-+]?\$?\s*(\b\d[\d,]*(?:\.\d+)?\b%?)", line)
        clean_nums = [n.replace(",", "").replace("$", "").strip() for n in nums]
        m_num = re.search(r"\$?\b\d", line)
        if m_num and clean_nums:
            row_label = line[:m_num.start()].replace("$", "").strip()
            if row_label:
                effective_section = current_section or top_header
                for c_idx, val in enumerate(clean_nums):
                    col_info = col_headers[c_idx] if c_idx < len(col_headers) else {}
                    cells.append({
                        "section": effective_section,
                        "row": row_label,
                        "value": val,
                        "col_idx": c_idx,
                        "duration": col_info.get("duration"),
                        "date": col_info.get("date"),
                        "raw_line": line,
                    })

    return cells


def verify_financial_cell_binding(
    chunk_text: str,
    answer_value: str,
    binding: FinancialQueryBinding,
) -> tuple[bool, str]:
    """Verifies that the cited answer value corresponds to an aligned cell matching the bound query requirements.

    Checks:
      1. Metric category alignment (rejects gross margin or cost of sales for net sales query).
      2. Row entity alignment (rejects competing products row for services query).
      3. Column duration alignment (rejects nine months for three months query).
      4. Column date alignment (rejects June 28, 2025 for June 27, 2026 query).
    """
    if not binding.is_active:
        return True, "inactive_binding"

    clean_ans = re.sub(r"[^\d.]", "", str(answer_value)).strip()
    if not clean_ans:
        return False, "no_numeric_answer"

    # Extract all candidate numbers from answer_value
    nums = re.findall(r"\b\d[\d,]*(?:\.\d+)?\b", str(answer_value))
    candidate_nums = [n.replace(",", "").strip() for n in nums if n.replace(",", "").strip()]
    if clean_ans not in candidate_nums:
        candidate_nums.append(clean_ans)

    cells = parse_financial_table_cells(chunk_text)
    if not cells:
        return False, "no_cells_found_in_chunk"

    has_matching_value = False

    for cand_val in candidate_nums:
        matching_cells = [c for c in cells if c["value"] == cand_val]
        if not matching_cells:
            continue

        has_matching_value = True

        for cell in matching_cells:
            sec = cell.get("section") or ""
            row = cell.get("row") or ""
            dur = cell.get("duration") or ""
            dt = cell.get("date") or ""

            # 1. Metric check
            if binding.metric == "net_sales":
                if "gross margin" in sec or "cost of sales" in sec or "operating expenses" in sec:
                    continue
                if not ("sales" in sec or "revenue" in sec or "operations" in chunk_text.lower()):
                    continue
            elif binding.metric == "gross_margin":
                if not ("gross margin" in sec or "gross profit" in sec):
                    continue
            elif binding.metric == "cost_of_sales":
                if not ("cost of sales" in sec or "cost of revenue" in sec or "cogs" in sec):
                    continue
            elif binding.metric == "operating_income":
                if not ("operating income" in sec or "operating profit" in sec):
                    continue
            elif binding.metric == "net_income":
                if not ("net income" in sec or "net earnings" in sec):
                    continue

            # 2. Entity check
            if binding.entity:
                if binding.entity.lower() not in row.lower():
                    continue

            # 3. Period duration check
            if binding.duration_norm:
                if "three" in binding.duration_norm and dur and "three" not in dur:
                    continue
                if "nine" in binding.duration_norm and dur and "nine" not in dur:
                    continue
                if "six" in binding.duration_norm and dur and "six" not in dur:
                    continue
                if "twelve" in binding.duration_norm and dur and "twelve" not in dur:
                    continue

            # 4. Period date check
            if binding.date_norm:
                yr_match = re.search(r"20\d{2}", binding.date_norm)
                if yr_match and dt and yr_match.group(0) not in dt:
                    continue
                day_match = re.search(r"\d{1,2}(?=,20\d{2})", binding.date_norm)
                if day_match and dt and day_match.group(0) not in dt:
                    continue
                month_match = re.search(
                    r"(january|february|march|april|may|june|july|august|september|october|november|december)",
                    binding.date_norm,
                )
                if month_match and dt and month_match.group(0) not in dt:
                    continue

            return True, "verified"

    if has_matching_value:
        return False, "cell_attributes_do_not_match_binding"

    return False, "value_not_found_in_cited_table"


def score_table_chunk_metric_affinity(chunk_text: str, binding: FinancialQueryBinding) -> float:
    """Computes an affinity adjustment score for table evidence selection based on bound requirements."""
    if not binding.is_active or not chunk_text:
        return 0.0

    text_lower = chunk_text.lower()
    score = 0.0

    if binding.metric == "net_sales":
        if "condensed consolidated statements of operations" in text_lower or "net sales:" in text_lower:
            score += 2.0
        if "gross margin" in text_lower and "statements of operations" not in text_lower:
            score -= 2.0
    elif binding.metric == "gross_margin":
        if "gross margin percentage" in text_lower or "products and services gross margin" in text_lower or text_lower.startswith("gross margin"):
            score += 3.0
        elif "gross margin" in text_lower:
            score += 1.0
        if "condensed consolidated statements of operations" in text_lower:
            score -= 1.0

    if binding.entity and binding.entity.lower() in text_lower:
        score += 0.5

    return score
