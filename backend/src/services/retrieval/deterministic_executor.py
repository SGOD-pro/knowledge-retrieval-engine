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

    def format_answer(self, query: str = "") -> str:
        q_lower = query.lower()
        if self.operator == ExecutionOperator.DIFFERENCE:
            if len(self.operands) >= 2:
                return f"{self.result_value} (calculated as {self.operands[0].normalized_value} minus {self.operands[1].normalized_value})"
            return f"{self.result_value}"
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
            delta_days = (d_a - d_b).days
            return ExecutionResult(
                result_value=Decimal(delta_days),
                operator=operator,
                operands=tuple(operands),
                input_binding_confidence=binding_conf,
                overall_confidence=overall_conf,
                provenance_citations=provenances,
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

            # First pass: check for exact phrase or all keywords in key
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

            # Strategy 3: Line-by-line search in chunk text
            lines = txt.splitlines()
            for line in lines:
                line_lower = line.lower()
                matches_line = term_clean in line_lower or all(kw in line_lower for kw in keywords)
                if matches_line:
                    num_matches = re.findall(r"(?<![A-Za-z0-9_])[-+]?\$?\s*[\d,]+(?:\.\d+)?(?:\s*[MBk%]|\s+million|\s+billion)?", line)
                    filtered = []
                    for nm in num_matches:
                        clean_nm = nm.strip().replace("$", "").replace(",", "")
                        if clean_nm in ("2023", "2024", "2025", "2026", "2027") and clean_nm not in keywords:
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
                                binding_confidence=0.90,
                            )
                        except Exception:
                            continue
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
        if diff_m:
            term1, term2 = diff_m.group(1).strip(), diff_m.group(2).strip()
            op1 = self._extract_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_operand_from_chunks(term2, chunks, query)
            if op1 and op2:
                return self.execute(ExecutionOperator.DIFFERENCE, [op1, op2])

        between_m = re.search(
            r"(?:.*?\b(?:calculate|find|compute|what\s+is)\s+)?(?:the\s+)?difference\s+between\s+([^,;]+?)\s+and\s+(.+?)(?:\s+in|\s+for|\s*\?|\.|$)",
            query,
            re.IGNORECASE,
        )
        if between_m:
            term1, term2 = between_m.group(1).strip(), between_m.group(2).strip()
            op1 = self._extract_operand_from_chunks(term1, chunks, query)
            op2 = self._extract_operand_from_chunks(term2, chunks, query)
            if op1 and op2:
                return self.execute(ExecutionOperator.DIFFERENCE, [op1, op2])

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
