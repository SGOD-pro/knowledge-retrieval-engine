"""Deterministic Executor for exact mathematical, statistical, and date operations.

All arithmetic uses Python Decimal for arbitrary precision.
Every operand retains explicit provenance and binding confidence.
Arithmetic is NEVER delegated to an LLM when operands are available.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
import re
from typing import Any


class ExecutionOperator(str, Enum):
    SUM = "sum"
    AVERAGE = "average"
    COUNT = "count"
    MIN = "min"
    MAX = "max"
    DIFFERENCE = "difference"
    PERCENTAGE_DIFFERENCE = "percentage_difference"
    PERCENTAGE_POINT_DIFFERENCE = "percentage_point_difference"
    RATIO = "ratio"
    DATE_DIFFERENCE = "date_difference"
    COMPARE = "compare"


class ExecutionError(Exception):
    """Raised when deterministic execution cannot compute a result."""
    pass


@dataclass(frozen=True)
class Operand:
    raw_value: Any
    normalized_value: Decimal | datetime | str
    source_citation: dict[str, Any] = field(default_factory=dict)
    binding_confidence: float = 1.0


@dataclass(frozen=True)
class ExecutionResult:
    result_value: Decimal | int | str | dict[str, Any]
    operator: ExecutionOperator
    operands: tuple[Operand, ...]
    execution_correctness: float = 1.0  # Deterministic math is exact
    input_binding_confidence: float = 1.0
    overall_confidence: float = 1.0
    provenance_citations: tuple[dict[str, Any], ...] = ()
    unit: str | None = None

    def format_answer(self, query: str = "") -> str:
        q_lower = query.lower()
        unit_str = f" {self.unit}" if self.unit else (" million dollars" if ("million" in q_lower or "dollar" in q_lower) else "")
        if self.operator == ExecutionOperator.DIFFERENCE:
            if len(self.operands) >= 2:
                op0_str = f"{self.operands[0].normalized_value}%" if (self.unit == "percentage points" and "%" not in str(self.operands[0].normalized_value)) else str(self.operands[0].normalized_value)
                op1_str = f"{self.operands[1].normalized_value}%" if (self.unit == "percentage points" and "%" not in str(self.operands[1].normalized_value)) else str(self.operands[1].normalized_value)
                return f"{self.result_value}{unit_str}: {op0_str} minus {op1_str}"
            return f"{self.result_value}{unit_str}"
        if self.operator == ExecutionOperator.DATE_DIFFERENCE:
            if len(self.operands) >= 2:
                try:
                    d1 = parse_date_value(self.operands[0].normalized_value)
                    d2 = parse_date_value(self.operands[1].normalized_value)
                    earlier, later = min(d1, d2), max(d1, d2)
                    return f"{int(self.result_value)} days, from {earlier.strftime('%-d %B %Y')} to {later.strftime('%-d %B %Y')}"
                except Exception:
                    pass
            return f"{int(self.result_value)} days"
        if self.operator == ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE:
            if len(self.operands) >= 2:
                return f"{self.result_value} percentage points ({self.operands[0].normalized_value}% minus {self.operands[1].normalized_value}%)"
            return f"{self.result_value} percentage points"
        if self.operator == ExecutionOperator.PERCENTAGE_DIFFERENCE:
            return f"{self.result_value:.2f}%"
        if self.operator == ExecutionOperator.RATIO:
            if "percent" in q_lower and len(self.operands) >= 2:
                return f"{self.result_value:.2f}%, calculated as {self.operands[0].normalized_value} / {self.operands[1].normalized_value} \u00d7 100"
            return f"{self.result_value}"
        return f"{self.result_value}"


def parse_decimal_value(val: Any) -> Decimal:
    """Safely parse formatted strings, currencies, percentages, and floats into Decimal."""
    if val is None:
        raise ExecutionError("Cannot convert None to Decimal")
    if isinstance(val, Decimal):
        return val
    if isinstance(val, (int, float)):
        return Decimal(str(val))

    s = str(val).strip()
    # Remove currency symbols and clean commas
    cleaned = re.sub(r"[$€£₹,\s]|Rs\.?", "", s)
    # Check percentage
    is_pct = False
    if cleaned.endswith("%"):
        is_pct = True
        cleaned = cleaned[:-1].strip()

    # Scale multiplier suffixes (e.g. 50M, 1.2B, 10k)
    multiplier = Decimal("1")
    if cleaned.endswith(("M", "m")):
        multiplier = Decimal("1000000")
        cleaned = cleaned[:-1].strip()
    elif cleaned.endswith(("B", "b")):
        multiplier = Decimal("1000000000")
        cleaned = cleaned[:-1].strip()
    elif cleaned.endswith(("k", "K")):
        multiplier = Decimal("1000")
        cleaned = cleaned[:-1].strip()

    try:
        dec = Decimal(cleaned) * multiplier
        return dec
    except InvalidOperation as e:
        raise ExecutionError(f"Could not parse numeric value '{val}': {e}") from e


def parse_date_value(val: Any) -> datetime:
    """Parse common date formats."""
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%B %Y",
        "%b %Y",
        "%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ExecutionError(f"Could not parse date value '{val}'")


class DeterministicExecutor:
    """Executes arithmetic and comparisons on verified operands with complete provenance."""

    def execute(
        self,
        operator: ExecutionOperator,
        operands: list[Operand],
        unit: str | None = None,
    ) -> ExecutionResult:
        if not operands:
            raise ExecutionError(f"Operator {operator} requires at least one operand")

        # Compute input binding confidence: minimum confidence among operands
        binding_conf = min((op.binding_confidence for op in operands), default=1.0)
        overall_conf = round(binding_conf * 1.0, 4)

        provenances = tuple(op.source_citation for op in operands if op.source_citation)

        if operator == ExecutionOperator.COUNT:
            return ExecutionResult(
                result_value=len(operands),
                operator=operator,
                operands=tuple(operands),
                input_binding_confidence=binding_conf,
                overall_confidence=overall_conf,
                provenance_citations=provenances,
            )

        if operator in (ExecutionOperator.SUM, ExecutionOperator.AVERAGE, ExecutionOperator.MIN, ExecutionOperator.MAX):
            dec_values = [parse_decimal_value(op.normalized_value) for op in operands]
            if operator == ExecutionOperator.SUM:
                res_val = sum(dec_values)
            elif operator == ExecutionOperator.AVERAGE:
                res_val = sum(dec_values) / Decimal(len(dec_values))
            elif operator == ExecutionOperator.MIN:
                res_val = min(dec_values)
            elif operator == ExecutionOperator.MAX:
                res_val = max(dec_values)

            return ExecutionResult(
                result_value=res_val,
                operator=operator,
                operands=tuple(operands),
                input_binding_confidence=binding_conf,
                overall_confidence=overall_conf,
                provenance_citations=provenances,
            )

        # Binary operators
        if len(operands) < 2:
            raise ExecutionError(f"Operator {operator} requires at least 2 operands, got {len(operands)}")

        if operator == ExecutionOperator.DATE_DIFFERENCE:
            d_a = parse_date_value(operands[0].normalized_value)
            d_b = parse_date_value(operands[1].normalized_value)
            delta_days = abs((d_a - d_b).days)
            return ExecutionResult(
                result_value=Decimal(delta_days),
                operator=operator,
                operands=tuple(operands),
                input_binding_confidence=binding_conf,
                overall_confidence=overall_conf,
                provenance_citations=provenances,
                unit="days",
            )

        a_dec = parse_decimal_value(operands[0].normalized_value)
        b_dec = parse_decimal_value(operands[1].normalized_value)

        if operator == ExecutionOperator.DIFFERENCE:
            res_val = a_dec - b_dec
        elif operator == ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE:
            res_val = a_dec - b_dec
        elif operator == ExecutionOperator.PERCENTAGE_DIFFERENCE:
            if b_dec == Decimal("0"):
                raise ExecutionError("Division by zero in percentage difference calculation")
            res_val = ((a_dec - b_dec) / abs(b_dec)) * Decimal("100")
        elif operator == ExecutionOperator.RATIO:
            if b_dec == Decimal("0"):
                raise ExecutionError("Division by zero in ratio calculation")
            res_val = a_dec / b_dec
        elif operator == ExecutionOperator.COMPARE:
            delta = a_dec - b_dec
            res_val = {
                "greater": a_dec > b_dec,
                "equal": a_dec == b_dec,
                "less": a_dec < b_dec,
                "delta": delta,
                "percent_delta": ((a_dec - b_dec) / abs(b_dec) * Decimal("100")) if b_dec != 0 else None,
            }
        else:
            raise ExecutionError(f"Unsupported operator: {operator}")

        return ExecutionResult(
            result_value=res_val,
            operator=operator,
            operands=tuple(operands),
            input_binding_confidence=binding_conf,
            overall_confidence=overall_conf,
            provenance_citations=provenances,
            unit=unit,
        )

    def _extract_operand_from_chunks(
        self, term: str, chunks: list[Any], query: str = ""
    ) -> Operand | None:
        term_clean = term.strip().lower()
        raw_words = re.findall(r"\b\w+\b", term_clean)
        stop_words = {
            "the", "of", "in", "and", "for", "from", "using", "balance", "sheet",
            "row", "rows", "calculate", "find", "what", "is", "value", "recorded",
        }
        keywords = [w for w in raw_words if w not in stop_words]
        if not keywords:
            keywords = raw_words
        if not keywords:
            return None

        years_in_query = re.findall(r"\b(19\d\d|20\d\d)\b", query)
        target_year = years_in_query[0] if years_in_query else None

        for chunk in chunks:
            txt = getattr(chunk, "text", "")
            if not txt:
                continue

            chunk_id = getattr(chunk, "id", "")
            doc_id = getattr(chunk, "document_id", "")
            citation = {"chunk_id": chunk_id, "document_id": doc_id} if chunk_id else {}

            # Strategy 1: Key-Value parsing
            kv_matches = re.findall(
                r"([A-Za-z0-9_\- /()]+?):\s*([^:\n]+?)(?=\s+[A-Za-z0-9_\- /()]+:|\. [A-Z]|\.?$)",
                txt.strip(),
            )
            if target_year:
                chunk_has_diff_year = False
                for k, v in kv_matches:
                    if k.strip().lower() == "year" and v.strip() != target_year:
                        chunk_has_diff_year = True
                        break
                if chunk_has_diff_year:
                    continue

            # First pass: check for schema-normalized entity-attribute-value pairs
            # e.g., Variable_name: Closing stocks ... Value: 88214
            var_name = None
            var_val = None
            for k, v in kv_matches:
                k_clean = k.strip().lower().replace("_", " ")
                v_clean = v.strip()
                if any(attr in k_clean for attr in ["variable name", "variable", "indicator", "metric", "description", "concept"]):
                    if term_clean in v_clean.lower() or all(kw in v_clean.lower() for kw in keywords):
                        var_name = v_clean
                if any(attr in k_clean for attr in ["value", "amount", "total", "data value"]):
                    var_val = v_clean
            if var_name and var_val:
                val_m = re.search(r"[-+]?\$?\s*([\d,]+(?:\.\d+)?)\s*(?:[MBk%]|\s+million|\s+billion)?", var_val)
                if val_m:
                    try:
                        dec_val = parse_decimal_value(val_m.group(0).strip())
                        return Operand(
                            raw_value=val_m.group(0).strip(),
                            normalized_value=dec_val,
                            source_citation=citation,
                            binding_confidence=0.95,
                        )
                    except Exception:
                        pass

            # Second pass: check for exact phrase or all keywords in key
            for k, v in kv_matches:
                k_lower = k.lower()
                if term_clean in k_lower or all(kw in k_lower for kw in keywords):
                    val_clean = v.strip().rstrip(".")
                    val_m = re.search(r"[-+]?\$?\s*([\d,]+(?:\.\d+)?)\s*(?:[MBk%]|\s+million|\s+billion)?", val_clean)
                    if val_m:
                        try:
                            dec_val = parse_decimal_value(val_m.group(0).strip())
                            return Operand(
                                raw_value=val_m.group(0).strip(),
                                normalized_value=dec_val,
                                source_citation=citation,
                                binding_confidence=0.95,
                            )
                        except Exception:
                            pass

            # Strategy 2: Tabular Grid Parsing (Markdown pipes or CSV commas)
            raw_lines = [l.strip() for l in txt.splitlines() if l.strip()]
            if len(raw_lines) >= 2:
                delim = "|" if "|" in raw_lines[0] else ("," if "," in raw_lines[0] else None)
                if delim:
                    header_line = raw_lines[0]
                    headers = [h.strip().strip("|").strip() for h in header_line.split(delim) if h.strip()]
                    query_stop = {"what", "is", "the", "difference", "between", "sum", "of", "and", "in", "for", "across"}
                    query_words = [w.lower() for w in re.findall(r"\b\w+\b", query) if w.lower() not in query_stop]

                    target_col_idx = None
                    for idx, h in enumerate(headers):
                        h_lower = h.lower()
                        if any(kw in h_lower for kw in keywords):
                            target_col_idx = idx
                            break
                    if target_col_idx is None:
                        for idx, h in enumerate(headers):
                            h_lower = h.lower()
                            if any(qw in h_lower for qw in query_words):
                                target_col_idx = idx
                                break

                    data_rows = raw_lines[1:]
                    if data_rows and not any(c.isalnum() for c in data_rows[0]):
                        data_rows = data_rows[1:]

                    for r in data_rows:
                        cells = [c.strip().strip("|").strip() for c in r.split(delim)]
                        cells = [c for c in cells if c or delim == ","]
                        if not cells:
                            continue
                        r_lower = r.lower()
                        if any(kw in r_lower for kw in keywords):
                            val_str = None
                            if target_col_idx is not None and target_col_idx < len(cells):
                                val_str = cells[target_col_idx]
                            else:
                                for cell in cells:
                                    if re.search(r"\b\d+(?:\.\d+)?\b", cell):
                                        val_str = cell
                                        break
                            if val_str:
                                try:
                                    dec_val = parse_decimal_value(val_str)
                                    return Operand(
                                        raw_value=val_str,
                                        normalized_value=dec_val,
                                        source_citation=citation,
                                        binding_confidence=0.95,
                                    )
                                except Exception:
                                    pass

            # Strategy 3: Line-by-line search in chunk text (strict structured/key-value lines only)
            lines = txt.splitlines()
            _DATE_MONTHS = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
            candidate_lines = []
            for line in lines:
                line_lower = line.lower()
                # Skip long narrative prose or introductory lines
                if len(line.split()) > 15 or "ended" in line_lower or "table shows" in line_lower or "refer to" in line_lower:
                    continue

                if term_clean in line_lower or all(kw in line_lower for kw in keywords):
                    # Score candidate line: prefer "Total <term>" or exact starts
                    score = 50
                    if line_lower.startswith("total " + term_clean) or line_lower.startswith("total " + " ".join(keywords)):
                        score = 100
                    elif line_lower.startswith(term_clean) or line_lower.startswith(" ".join(keywords)):
                        score = 90
                    elif "total" in line_lower:
                        score = 80
                    elif "other " in line_lower:
                        score = 10
                    candidate_lines.append((score, line))

            candidate_lines.sort(key=lambda x: x[0], reverse=True)
            for _, line in candidate_lines:
                # Remove date patterns like "June 27" or "March 16" so day numbers are not extracted as operands
                clean_line = re.sub(_DATE_MONTHS + r"\s+\d+", " ", line, flags=re.IGNORECASE)
                num_matches = re.findall(r"(?<![A-Za-z0-9_])[-+]?\$?\s*[\d,]+(?:\.\d+)?(?:\s*[MBk%]|\s+million|\s+billion)?", clean_line)
                filtered = []
                for nm in num_matches:
                    clean_nm = nm.strip().replace("$", "").replace(",", "")
                    if clean_nm in ("2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027", "2028") and clean_nm not in keywords:
                        continue
                    filtered.append(nm.strip())
                if filtered:
                    candidate = filtered[0]
                    try:
                        dec_val = parse_decimal_value(candidate)
                        return Operand(
                            raw_value=candidate,
                            normalized_value=dec_val,
                            source_citation=citation,
                            binding_confidence=0.95,
                        )
                    except Exception:
                        continue
        return None

    def _extract_date_operand_from_chunks(
        self, term: str, chunks: list[Any], query: str = ""
    ) -> Operand | None:
        term_clean = term.strip().lower()
        keywords = [w for w in re.findall(r"\w+", term_clean) if w not in {"the", "of", "and", "in", "date", "dates", "bill", "passage"}]
        for chunk in chunks:
            txt = getattr(chunk, "text", "")
            if not txt:
                continue
            chunk_id = getattr(chunk, "id", "")
            doc_id = getattr(chunk, "document_id", "")
            citation = {"chunk_id": chunk_id, "document_id": doc_id} if chunk_id else {}

            kv_matches = re.findall(
                r"(?:^|[.\n]\s*)([A-Za-z0-9_\- /()]+?(?:No\.)?[A-Za-z0-9_\- /()]*?):\s*([^:\n]+?)(?=[.\n]|$)",
                txt.strip(),
            )
            scored_candidates = []
            for k, v in kv_matches:
                k_clean = k.lower().replace("_", " ").strip(". ")
                m = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", v)
                if m:
                    is_rs = "rajya" in k_clean or bool(re.search(r"\brs\b", k_clean))
                    is_ls = "lok" in k_clean or bool(re.search(r"\bls\b", k_clean))
                    if "rajya" in keywords and not is_rs:
                        continue
                    if "lok" in keywords and not is_ls:
                        continue
                    if "assent" in keywords and "assent" not in k_clean:
                        continue
                    score = sum(1 for kw in keywords if kw in k_clean)
                    if "rajya" in keywords and is_rs:
                        score += 2
                    if "lok" in keywords and is_ls:
                        score += 2
                    if "assent" in keywords and "assent" in k_clean:
                        score += 2
                    if score > 0:
                        scored_candidates.append((score, m.group(0)))
            if scored_candidates:
                scored_candidates.sort(key=lambda x: x[0], reverse=True)
                raw_d = scored_candidates[0][1]
                try:
                    dt = parse_date_value(raw_d)
                    return Operand(
                        raw_value=raw_d,
                        normalized_value=raw_d,
                        source_citation=citation,
                        binding_confidence=0.95,
                    )
                except Exception:
                    pass
        return None

    def resolve_and_execute(self, query: str, chunks: list[Any]) -> ExecutionResult | None:
        """Attempt to deterministically extract operands and execute arithmetic for a query."""
        if not query:
            return None

        q_lower = query.lower()
        has_math_intent = any(
            w in q_lower
            for w in [
                "minus", "subtract", "difference between", "diff between",
                "percentage difference", "percentage point", "percentage points",
                "percentage of", "ratio of", "ratio between", "sum of", "average of",
                "days elapsed", "days between", "calendar days", "elapsed between",
            ]
        )
        if not has_math_intent:
            return None

        citation_fallback = {}
        if chunks:
            citation_fallback = {
                "chunk_id": getattr(chunks[0], "id", ""),
                "document_id": getattr(chunks[0], "document_id", ""),
            }

        # 1. Direct numerical operands in the query
        direct_minus = re.search(
            r"([+-]?\$?[\d,]+(?:\.\d+)?%?)\s+(?:minus|-)\s+([+-]?\$?[\d,]+(?:\.\d+)?%?)",
            query,
            re.IGNORECASE,
        )
        if direct_minus:
            try:
                raw1 = direct_minus.group(1).strip()
                raw2 = direct_minus.group(2).strip()
                op1 = Operand(
                    raw_value=raw1,
                    normalized_value=parse_decimal_value(raw1),
                    source_citation=citation_fallback,
                )
                op2 = Operand(
                    raw_value=raw2,
                    normalized_value=parse_decimal_value(raw2),
                    source_citation=citation_fallback,
                )
                if "percentage point" in q_lower:
                    return self.execute(ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE, [op1, op2])
                if "percentage difference" in q_lower:
                    return self.execute(ExecutionOperator.PERCENTAGE_DIFFERENCE, [op1, op2])
                return self.execute(ExecutionOperator.DIFFERENCE, [op1, op2])
            except Exception:
                pass

        # 2. Percentage point difference between named concepts
        if "percentage point" in q_lower:
            m = re.search(
                r"(?:percentage\s+points?\s+(?:difference\s+between\s+)?|difference\s+in\s+percentage\s+points\s+between\s+)(.+?)\s+(?:and|minus)\s+(.+)",
                q_lower,
            )
            if m:
                term1, term2 = m.group(1).strip(), m.group(2).strip()
                op1 = self._extract_operand_from_chunks(term1, chunks, query)
                op2 = self._extract_operand_from_chunks(term2, chunks, query)
                if op1 and op2:
                    return self.execute(ExecutionOperator.PERCENTAGE_POINT_DIFFERENCE, [op1, op2])

        # 3. Percentage of total (Ratio * 100)
        pct_of_m = re.search(
            r"what\s+percentage\s+of\s+(.+?)\s+(?:came\s+from|is|was|are|represents?)\s+(.+?)(?:\s+in|\s+for|\s*\?|$)",
            query,
            re.IGNORECASE,
        )
        if pct_of_m:
            total_term = pct_of_m.group(1).strip()
            part_term = pct_of_m.group(2).strip()
            op_total = self._extract_operand_from_chunks(total_term, chunks, query)
            op_part = self._extract_operand_from_chunks(part_term, chunks, query)
            if op_part and op_total:
                try:
                    ratio_res = self.execute(ExecutionOperator.RATIO, [op_part, op_total])
                    pct_dec = (ratio_res.result_value * Decimal("100")).quantize(Decimal("0.01"))
                    # Sanity check: a component percentage of a total cannot exceed 100% or be negative
                    if Decimal("0") <= pct_dec <= Decimal("100.0"):
                        return ExecutionResult(
                            result_value=pct_dec,
                            operator=ExecutionOperator.RATIO,
                            operands=(op_part, op_total),
                            input_binding_confidence=ratio_res.input_binding_confidence,
                            overall_confidence=ratio_res.overall_confidence,
                            provenance_citations=ratio_res.provenance_citations,
                        )
                except Exception:
                    pass

        # 4. Difference / Subtraction between named concepts
        diff_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?([^,;]+?)\s+(?:minus|-)\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if not diff_m and "minus" in q_lower:
            diff_m = re.search(
                r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?(.+?)\s+(?:minus|-)\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
                query,
                re.IGNORECASE,
            )
        if diff_m:
            term1, term2 = diff_m.group(1).strip(), diff_m.group(2).strip()
            op1 = self._extract_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_operand_from_chunks(term2, chunks, query)
            # Guard: ignore trivial/casual matches where both operands normalized to 1 or 0
            if op1 and op2 and not (op1.normalized_value in (0, 1) and op2.normalized_value in (0, 1)):
                detected_unit = None
                for c in chunks:
                    c_txt = getattr(c, "text", "").lower()
                    if "in millions" in c_txt or "dollars (millions)" in c_txt or "(in millions" in c_txt:
                        detected_unit = "million dollars"
                        break
                return self.execute(ExecutionOperator.DIFFERENCE, [op1, op2], unit=detected_unit)

        # Date difference in calendar days
        date_diff_m = re.search(
            r"(?:how\s+many\s+)?(?:calendar\s+)?days\s+(?:elapsed\s+)?between\s+([^,;]+?)\s+and\s+(.+?)(?:\s+for|\s+in|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if date_diff_m:
            term1, term2 = date_diff_m.group(1).strip(), date_diff_m.group(2).strip()
            op1 = self._extract_date_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_date_operand_from_chunks(term2, chunks, query)
            if op1 and op2:
                return self.execute(ExecutionOperator.DATE_DIFFERENCE, [op1, op2])

        between_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?(?:the\s+)?(?:difference|gap)\s+(?:in\s+[^,;]+?\s+)?between\s+([^,;]+?)\s+and\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if between_m:
            term1, term2 = between_m.group(1).strip(), between_m.group(2).strip()
            op1 = self._extract_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_operand_from_chunks(term2, chunks, query)
            if op1 and op2:
                detected_unit = None
                if "%" in str(op1.raw_value) or "%" in str(op2.raw_value) or "gap" in q_lower or "percentage point" in q_lower:
                    detected_unit = "percentage points"
                return self.execute(ExecutionOperator.DIFFERENCE, [op1, op2], unit=detected_unit)

        subtract_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute)\s+)?subtract\s+([^,;]+?)\s+from\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if subtract_m:
            term_sub, term_from = subtract_m.group(1).strip(), subtract_m.group(2).strip()
            op_sub = self._extract_operand_from_chunks(term_sub, chunks, query)
            op_from = self._extract_operand_from_chunks(term_from, chunks, query)
            if op_sub and op_from:
                return self.execute(ExecutionOperator.DIFFERENCE, [op_from, op_sub])

        # 5. Sum of concept/column across entities
        sum_across_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?(?:the\s+)?sum\s+of\s+([^,;]+?)\s+across\s+([^,;]+?)\s+and\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if sum_across_m:
            concept = sum_across_m.group(1).strip()
            ent1 = sum_across_m.group(2).strip()
            ent2 = sum_across_m.group(3).strip()
            op1 = self._extract_operand_from_chunks(f"{concept} {ent1}", chunks, query)
            op2 = self._extract_operand_from_chunks(f"{concept} {ent2}", chunks, query)
            if op1 and op2:
                return self.execute(ExecutionOperator.SUM, [op1, op2])

        sum_and_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?(?:the\s+)?sum\s+of\s+([^,;]+?)\s+and\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if sum_and_m:
            term1, term2 = sum_and_m.group(1).strip(), sum_and_m.group(2).strip()
            op1 = self._extract_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_operand_from_chunks(term2, chunks, query)
            if op1 and op2:
                return self.execute(ExecutionOperator.SUM, [op1, op2])

        return None


def _bind_subgoal_operand(
    sg: dict[str, Any],
    chunks: list[Any],
) -> tuple[Operand, str | None, set[str], Any] | None:
    """Bind a requirement subgoal to the best-matching numeric Operand and source chunk."""
    q_txt = str(sg.get("query_text") or "")
    q_low = q_txt.lower()
    req_years = {y for y in re.findall(r"\b(19\d{2}|20\d{2})\b", q_low)}
    req_codes = re.findall(r"\b([A-Za-z]\d{1,4})\b", q_txt)
    q_tokens = {
        t for t in re.findall(r"[a-z0-9]+", q_low)
        if len(t) >= 3 and t not in {"the", "and", "for", "with", "from", "value", "values"}
    }

    best_score = -1
    best_match: tuple[Operand, str | None, set[str], Any] | None = None

    def _score_chunk(c: Any, txt_low: str, matched_balance_sheet: bool) -> int:
        c_tokens = set(re.findall(r"[a-z0-9]+", txt_low))
        fmt = str(getattr(c, "source_format", "") or "").lower()
        if fmt:
            c_tokens.add(fmt)
        score = len(q_tokens & c_tokens)
        if matched_balance_sheet and ("balance sheet" in q_low or "10q" in q_low or "10k" in q_low or fmt == "pdf"):
            score += 2
        if ("csv" in q_low or "xlsx" in q_low or "survey" in q_low) and fmt in {"csv", "xlsx"}:
            score += 2
        return score

    for c in chunks:
        txt = str(getattr(c, "text", "") or "")
        txt_low = txt.lower()
        if req_years and not (req_years <= set(re.findall(r"\b(19\d{2}|20\d{2})\b", txt_low))):
            continue
        if req_codes and not all(re.search(rf"\b{re.escape(code.lower())}\b", txt_low) for code in req_codes):
            continue
        if "current ratio" in q_low and re.search(r"\bnot\s+current\s+ratio\b", txt_low):
            continue

        candidate: tuple[Operand, str | None, set[str], Any] | None = None
        matched_bs = False

        # 1. Balance-sheet current ratio: compute (current assets / current liabilities) * 100
        if "current ratio" in q_low and "current assets" in txt_low and "current liabilities" in txt_low:
            m_assets = re.search(r"current\s+assets[^\d\n]*\$?\s*([\d,]+(?:\.\d+)?)", txt_low)
            m_liab = re.search(r"current\s+liabilities[^\d\n]*\$?\s*([\d,]+(?:\.\d+)?)", txt_low)
            if m_assets and m_liab:
                a_val = parse_decimal_value(m_assets.group(1))
                l_val = parse_decimal_value(m_liab.group(1))
                if l_val != 0:
                    ratio_pct = (a_val / l_val) * Decimal("100")
                    raw_set = {
                        str(a_val).rstrip("0").rstrip("."),
                        str(l_val).rstrip("0").rstrip("."),
                        str(ratio_pct.quantize(Decimal("0.01"))).rstrip("0").rstrip("."),
                    }
                    candidate = (
                        Operand(raw_value=f"{a_val}/{l_val}", normalized_value=ratio_pct),
                        "percentage",
                        raw_set,
                        c,
                    )
                    matched_bs = True

        # 2. Structured CSV/XLSX row ("Value: <num>" or "value <num>" and optional "Units: <unit>")
        if candidate is None:
            m_val = re.search(r"\bvalue\s*[:\s]\s*(-?[\d,]+(?:\.\d+)?)", txt_low)
            if m_val:
                val_dec = parse_decimal_value(m_val.group(1))
                unit: str | None = None
                m_unit = re.search(r"\bunits?\s*:\s*([a-z0-9 %()$]+)", txt_low)
                if m_unit:
                    u_str = m_unit.group(1).strip()
                    if "percent" in u_str or "%" in u_str:
                        unit = "percentage"
                    elif "dollar" in u_str or "$" in u_str:
                        unit = "dollars"
                candidate = (
                    Operand(raw_value=m_val.group(1), normalized_value=val_dec),
                    unit,
                    {str(val_dec).rstrip("0").rstrip(".")},
                    c,
                )

        # 3. Direct percentage/value near metric
        if candidate is None and "current ratio" in q_low and "current ratio" in txt_low:
            m_pct = re.search(r"current\s+ratio[^\d\n]*(-?[\d,]+(?:\.\d+)?)\s*%", txt_low)
            if m_pct:
                val_dec = parse_decimal_value(m_pct.group(1))
                candidate = (
                    Operand(raw_value=m_pct.group(0), normalized_value=val_dec),
                    "percentage",
                    {str(val_dec).rstrip("0").rstrip(".")},
                    c,
                )

        if candidate is not None:
            sc = _score_chunk(c, txt_low, matched_bs)
            if sc > best_score:
                best_score = sc
                best_match = candidate

    return best_match


def _fmt_dec_variants(val: Decimal) -> set[str]:
    out = {str(val).rstrip("0").rstrip(".") or "0"}
    for q in ("0.1", "0.01"):
        s_raw = str(val.quantize(Decimal(q)))
        out.add(s_raw)
        out.add(s_raw.rstrip("0").rstrip(".") or "0")
    return out


def verify_two_operand_comparison(
    query: str,
    subgoal_specs: list[dict[str, Any]],
    chunks: list[Any],
    answer: str,
) -> tuple[bool, str, set[str]]:
    """Bind two comparison operands from evidence, run DeterministicExecutor, and validate answer direction & delta."""
    if len(subgoal_specs) != 2 or not chunks:
        return True, "not_applicable", set()

    q_low = query.lower()
    if not re.search(r"\b(?:compare|difference|percentage\s+point|percentage\s+difference|higher|lower)\b", q_low):
        return True, "not_comparison", set()

    b1 = _bind_subgoal_operand(subgoal_specs[0], chunks)
    b2 = _bind_subgoal_operand(subgoal_specs[1], chunks)
    if b1 is None or b2 is None:
        return False, "unresolved_comparison_operands", set()

    op1, unit1, raw1, _ = b1
    op2, unit2, raw2, _ = b2
    if unit1 and unit2 and unit1 != unit2:
        return False, f"incompatible_units:{unit1}_vs_{unit2}", set()

    executor = DeterministicExecutor()
    res = executor.execute(ExecutionOperator.COMPARE, [op1, op2], unit=unit1 or unit2)
    cmp_dict = res.result_value
    a_dec = parse_decimal_value(op1.normalized_value)
    b_dec = parse_decimal_value(op2.normalized_value)
    abs_delta = abs(a_dec - b_dec)
    rel_pct = abs(cmp_dict["percent_delta"]) if cmp_dict.get("percent_delta") is not None else None

    derived_nums: set[str] = set()
    derived_nums |= _fmt_dec_variants(a_dec) | _fmt_dec_variants(b_dec) | _fmt_dec_variants(abs_delta) | raw1 | raw2
    if rel_pct is not None:
        derived_nums |= _fmt_dec_variants(rel_pct)

    # Extract distinguishing subject tokens for sub_1 vs sub_2
    _subj_stop = {
        "the", "and", "for", "with", "from", "that", "this", "value", "values",
        "ratio", "current", "percentage", "percent", "points", "scale", "same",
        "june", "july", "january", "march", "april", "may", "august", "september",
        "october", "november", "december",
    }
    def _subj_tokens(sg: dict[str, Any]) -> set[str]:
        toks = set(re.findall(r"[a-z0-9]{3,}", str(sg.get("query_text") or "").lower()))
        return {t for t in toks if t not in _subj_stop and not t.isdigit()}

    t1 = _subj_tokens(subgoal_specs[0])
    t2 = _subj_tokens(subgoal_specs[1])
    s1_only = t1 - t2
    s2_only = t2 - t1

    pos_pat = re.compile(r"\b(?:higher|greater|larger|exceeds|more\s+than|above)\b", re.IGNORECASE)
    neg_pat = re.compile(r"\b(?:lower|less|smaller|below|under)\b", re.IGNORECASE)
    eq_pat = re.compile(r"\b(?:equal|identical|the\s+same)\b", re.IGNORECASE)
    negation_pat = re.compile(r"\b(?:not|neither|no|never)\b", re.IGNORECASE)

    known_operand_strs = _fmt_dec_variants(a_dec) | _fmt_dec_variants(b_dec) | raw1 | raw2
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", answer) if s.strip()]
    for sent in sentences:
        sent_low = sent.lower()
        has_pos = bool(pos_pat.search(sent_low))
        has_neg = bool(neg_pat.search(sent_low))
        has_eq = bool(eq_pat.search(sent_low))

        # 1. Direction check when comparison language appears without negation
        if (has_pos or has_neg or has_eq) and not negation_pat.search(sent_low):
            if cmp_dict["equal"] and (has_pos or has_neg):
                return False, "direction_contradiction_equal_values", derived_nums

            # Identify grammatical subject before the direction word (or before "than")
            dir_m = pos_pat.search(sent_low) or neg_pat.search(sent_low)
            if dir_m:
                prefix_low = sent_low[:dir_m.start()]
                has_s1_subj = any(re.search(rf"\b{re.escape(t)}\b", prefix_low) for t in s1_only) if s1_only else False
                has_s2_subj = any(re.search(rf"\b{re.escape(t)}\b", prefix_low) for t in s2_only) if s2_only else False
                # Handle "Acme's current ratio is 200% and Retail H36 is 84%. Acme's ratio is lower..."
                if not has_s1_subj and not has_s2_subj and not re.search(r"\bthan\b", sent_low):
                    has_s1_subj = bool(s1_only)
                if has_s1_subj and not has_s2_subj:
                    if cmp_dict["greater"] and has_neg:
                        return False, "direction_contradiction_s1_greater", derived_nums
                    if cmp_dict["less"] and has_pos:
                        return False, "direction_contradiction_s1_less", derived_nums
                elif has_s2_subj and not has_s1_subj:
                    if cmp_dict["greater"] and has_pos:
                        return False, "direction_contradiction_s2_less", derived_nums
                    if cmp_dict["less"] and has_neg:
                        return False, "direction_contradiction_s2_greater", derived_nums

        # 2. Difference & scale check
        if has_pos or has_neg or re.search(r"\b(?:percentage\s+points?|pp|difference|change|relative)\b", sent_low):
            sent_clean = re.sub(r"\b[A-Za-z]\d{1,4}\b", " ", sent)
            raw_nums = re.findall(r"(?<![A-Za-z0-9.])(\d[\d,]*(?:\.\d+)?)\s*(%?)", sent_clean)
            for num_str, pct_sign in raw_nums:
                norm_s = num_str.replace(",", "")
                if len(norm_s) == 4 and norm_s.isdigit() and 1900 <= int(norm_s) <= 2099:
                    continue
                if norm_s in known_operand_strs:
                    continue
                try:
                    d_val = Decimal(norm_s)
                except InvalidOperation:
                    continue
                is_pp_phrase = bool(re.search(rf"{re.escape(num_str)}\s*(?:percentage\s+points?|pp\b)", sent_low))
                if is_pp_phrase:
                    if abs(d_val - abs_delta) > Decimal("0.15"):
                        return False, f"wrong_percentage_point_delta:{d_val}_expected_{abs_delta}", derived_nums
                elif pct_sign == "%" or "relative" in sent_low:
                    ok_rel = rel_pct is not None and abs(d_val - rel_pct) <= Decimal("0.15")
                    ok_abs = abs(d_val - abs_delta) <= Decimal("0.15") and not ("relative" in sent_low)
                    if not (ok_rel or ok_abs):
                        return False, f"wrong_relative_delta:{d_val}", derived_nums
                else:
                    if abs(d_val - abs_delta) > Decimal("0.15"):
                        return False, f"wrong_difference:{d_val}_expected_{abs_delta}", derived_nums

    return True, "verified", derived_nums


def _format_clean_decimal(val: Decimal) -> str:
    if val == val.to_integral_value():
        return str(int(val))
    return str(val.quantize(Decimal("0.01"))).rstrip("0").rstrip(".") or "0"


def evaluate_two_operand_comparison(
    query: str,
    subgoal_specs: list[dict[str, Any]],
    chunks: list[Any],
    snapshot_sources: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Evaluate a two-operand numeric comparison: verified render, unverified refusal, or unsupported."""
    if len(subgoal_specs) != 2:
        return {"supported": False, "status": "unsupported"}

    q_low = query.lower()
    if not re.search(r"\b(?:compare|difference|percentage\s+point|percentage\s+difference|higher|lower)\b", q_low):
        return {"supported": False, "status": "unsupported"}

    b1 = _bind_subgoal_operand(subgoal_specs[0], chunks) if chunks else None
    b2 = _bind_subgoal_operand(subgoal_specs[1], chunks) if chunks else None

    def _is_numeric_subgoal(sg: dict[str, Any]) -> bool:
        txt = str(sg.get("query_text") or "")
        return bool(
            "current ratio" in txt.lower()
            or re.search(r"\b[A-Za-z]\d{1,4}\b", txt)
            or "percentage point" in q_low
        )

    is_supported_path = (
        b1 is not None
        or b2 is not None
        or _is_numeric_subgoal(subgoal_specs[0])
        or _is_numeric_subgoal(subgoal_specs[1])
    )
    if not is_supported_path:
        return {"supported": False, "status": "unsupported"}

    unverified: list[dict[str, str]] = []
    if b1 is None:
        unverified.append({
            "subgoal_id": str(subgoal_specs[0].get("subgoal_id") or subgoal_specs[0].get("id") or "sub_1"),
            "query_text": str(subgoal_specs[0].get("query_text") or "").strip(),
            "reason": "operand_not_found",
        })
    if b2 is None:
        unverified.append({
            "subgoal_id": str(subgoal_specs[1].get("subgoal_id") or subgoal_specs[1].get("id") or "sub_2"),
            "query_text": str(subgoal_specs[1].get("query_text") or "").strip(),
            "reason": "operand_not_found",
        })
    if unverified:
        return {"supported": True, "status": "unverified", "unverified_operands": unverified}

    op1, unit1, _, c1 = b1
    op2, unit2, _, c2 = b2
    if unit1 and unit2 and unit1 != unit2:
        return {
            "supported": True,
            "status": "unverified",
            "unverified_operands": [{"subgoal_id": "unit_mismatch", "query_text": f"{unit1} vs {unit2}", "reason": "unit_mismatch"}],
        }

    # Enforce distinct eligible source coverage when >=2 explicit files were named
    if snapshot_sources:
        all_elig = {
            str(e).strip().lower()
            for sg in subgoal_specs
            for e in (sg.get("eligible_sources") or [])
            if str(e).strip()
        }
        if len(all_elig) >= 2:
            cited_srcs = {
                str(snapshot_sources.get(str(getattr(c, "document_id", "") or ""), "") or "").lower()
                for c in (c1, c2)
            } - {""}
            missing_elig = [e for e in sorted(all_elig) if e not in cited_srcs]
            if missing_elig:
                return {
                    "supported": True,
                    "status": "unverified",
                    "unverified_operands": [
                        {"subgoal_id": "source_coverage", "query_text": e, "reason": "missing_eligible_source"}
                        for e in missing_elig
                    ],
                }

    eff_unit = unit1 or unit2
    if eff_unit is None and ("percentage point" in q_low or "current ratio" in q_low or "%" in q_low):
        eff_unit = "percentage"
    is_pct = eff_unit == "percentage"
    executor = DeterministicExecutor()
    res = executor.execute(ExecutionOperator.COMPARE, [op1, op2], unit=eff_unit)
    cmp_dict = res.result_value
    a_dec = parse_decimal_value(op1.normalized_value)
    b_dec = parse_decimal_value(op2.normalized_value)
    abs_delta = abs(a_dec - b_dec)
    rel_pct = abs(cmp_dict["percent_delta"]) if cmp_dict.get("percent_delta") is not None else None

    pct_sfx = "%" if is_pct else ""
    v1_str = f"{_format_clean_decimal(a_dec)}{pct_sfx}"
    v2_str = f"{_format_clean_decimal(b_dec)}{pct_sfx}"

    def _clean_label(raw: Any, fallback: str) -> str:
        lbl = str(raw or fallback).strip().rstrip(".?!")
        return re.sub(r"\s+(?:in\s+percentage\s+points?|by\s+percentage\s+difference)$", "", lbl, flags=re.IGNORECASE).strip()

    s1_label = _clean_label(subgoal_specs[0].get("query_text"), "First value")
    s2_label = _clean_label(subgoal_specs[1].get("query_text"), "Second value")

    if cmp_dict["equal"]:
        pp_sfx = " percentage points" if is_pct else ""
        answer = f"{s1_label} ({v1_str}) equals {s2_label} ({v2_str}), a difference of 0{pp_sfx}."
    else:
        dir_word = "higher" if cmp_dict["greater"] else "lower"
        wants_rel = bool(re.search(r"\b(?:percentage\s+difference|percent\s+change|relative\s+percent)\b", q_low))
        if wants_rel and rel_pct is not None:
            diff_str = f"{_format_clean_decimal(rel_pct)}% (relative change)"
        elif is_pct:
            diff_str = f"{_format_clean_decimal(abs_delta)} percentage points"
        else:
            diff_str = _format_clean_decimal(abs_delta)
        answer = f"{s1_label} ({v1_str}) is {diff_str} {dir_word} than {s2_label} ({v2_str})."

    cited_chunks = [c1] if str(c1.id) == str(c2.id) else [c1, c2]
    return {
        "supported": True,
        "status": "verified",
        "answer": answer,
        "cited_chunks": cited_chunks,
        "unverified_operands": [],
    }


def render_verified_two_operand_comparison(
    query: str,
    subgoal_specs: list[dict[str, Any]],
    chunks: list[Any],
    snapshot_sources: dict[str, str] | None = None,
) -> tuple[str, list[Any]] | None:
    """Deterministically compute and render a supported two-operand numeric comparison with source citations."""
    res = evaluate_two_operand_comparison(query, subgoal_specs, chunks, snapshot_sources)
    if res.get("status") == "verified":
        return res["answer"], res["cited_chunks"]
    return None
