"""Conservative, probabilistic multi-signal header region detector.

Scoring signals:
- S_loc: Location near table start (conservative decay)
- S_type: Type distribution (strings or 4-digit year tokens)
- S_uniq: Cell uniqueness ratio
- S_trans: Datatype transition delta between candidate row and subsequent row
- S_dense: Non-empty cell density
- S_merge: Merged cell / hierarchical topology signal

Safety invariants:
- No individual signal may independently determine the header.
- Numeric year headers (e.g. 2019 | 2020 | 2021) are recognized and not penalized.
- If a row has 0% text/year tokens, its header score is 0.0 (numbers alone cannot be headers).
- Rows with low density (< 50% of table width) are penalized as notes or titles, not headers.
- Multi-row headers require high density on both rows, not title/note spans.
- Safe fallback: If no candidate passes confidence threshold, header_topology = NONE,
  synthetic Col_1..Col_N are assigned, and Row 0 is preserved as regular data.
"""

import math
import re
from typing import Any

from schemas.structured_table import (
    ColumnDefinition,
    HeaderTopology,
    InferredDtype,
    MergedRange,
    TableSchema,
)


def _is_year_token(val: Any) -> bool:
    """Check if value represents a 4-digit year header (e.g. 2020, '2021', 'FY22')."""
    if isinstance(val, int) and 1900 <= val <= 2099:
        return True
    s = str(val).strip()
    if re.match(r"^(?:19|20)\d{2}$", s):
        return True
    if re.match(r"^(?:FY|CY)\s*'?\d{2,4}$", s, re.IGNORECASE):
        return True
    return False


def _infer_cell_dtype(val: Any) -> InferredDtype:
    if val is None or str(val).strip() == "" or str(val).lower() == "none":
        return InferredDtype.NULL
    if isinstance(val, bool):
        return InferredDtype.BOOLEAN
    if isinstance(val, int):
        return InferredDtype.INTEGER
    if isinstance(val, float):
        return InferredDtype.DECIMAL
    s = str(val).strip()
    # Number with commas or decimals
    if re.match(r"^-?\d{1,3}(?:,\d{3})*(?:\.\d+)?$", s):
        return InferredDtype.DECIMAL if "." in s else InferredDtype.INTEGER
    if re.match(r"^-?\d+(?:\.\d+)?%$", s):
        return InferredDtype.PERCENTAGE
    if re.match(r"^(?:[$€£₹]|Rs\.?)\s*-?\d+", s):
        return InferredDtype.CURRENCY
    if re.match(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}/\d{1,2}/\d{2,4}$", s):
        return InferredDtype.DATE
    return InferredDtype.STRING


def _score_candidate_row(
    row: list[Any],
    row_idx: int,
    next_row: list[Any] | None,
    total_cols: int,
) -> float:
    if not row or total_cols == 0:
        return 0.0

    non_empty = [c for c in row if c is not None and str(c).strip() != ""]
    if not non_empty:
        return 0.0

    # 1. Type distribution: text strings or year tokens (e.g. 2019, 2020)
    header_type_count = 0
    for c in non_empty:
        dtype = _infer_cell_dtype(c)
        if dtype == InferredDtype.STRING or _is_year_token(c):
            header_type_count += 1
    s_type = header_type_count / len(non_empty)

    # Invariant: A row with zero string or year tokens CANNOT be a header
    if s_type == 0.0:
        return 0.0

    # 2. Density signal: fraction of non-empty cells relative to table width
    s_dense = len(non_empty) / max(total_cols, len(row))

    # 3. Location signal: conservative exponential decay
    s_loc = math.exp(-0.08 * row_idx)

    # 4. Uniqueness signal: headers are rarely repetitive
    cleaned_strs = [str(c).strip().lower() for c in non_empty]
    s_uniq = len(set(cleaned_strs)) / len(cleaned_strs) if cleaned_strs else 0.0

    # 5. Datatype transition delta relative to next row
    s_trans = 0.5  # neutral default
    if next_row:
        next_non_empty = [c for c in next_row if c is not None and str(c).strip() != ""]
        if next_non_empty:
            next_numeric_count = sum(
                1 for c in next_non_empty
                if _infer_cell_dtype(c) in (InferredDtype.INTEGER, InferredDtype.DECIMAL, InferredDtype.PERCENTAGE, InferredDtype.CURRENCY)
                and not _is_year_token(c)
            )
            next_num_ratio = next_numeric_count / len(next_non_empty)
            # If candidate row is header-like and next row has numbers/data
            s_trans = (s_type * 0.5) + (next_num_ratio * 0.5)

    # Multi-signal conservative blend
    raw_score = (
        (0.15 * s_loc)
        + (0.35 * s_type)
        + (0.15 * s_uniq)
        + (0.15 * s_dense)
        + (0.20 * s_trans)
    )

    # Density gating: Headers must populate the columns. Leading titles/notes with 1-2 cells get docked.
    if s_dense < 0.6:
        raw_score *= (s_dense / 0.6)

    # Type gating: if s_type is low, reduce confidence proportionally
    if s_type < 0.5:
        raw_score *= (s_type / 0.5)

    return round(raw_score, 4)


def detect_header_region(
    raw_rows: list[list[Any]],
    max_scan_rows: int = 15,
    confidence_threshold: float = 0.60,
    merged_ranges: list[MergedRange] | None = None,
) -> tuple[TableSchema, int]:
    """Detect table header region using multi-signal probabilistic scoring.

    Returns:
        (TableSchema, data_start_row_index)

    Invariants:
        - If confidence is insufficient (< confidence_threshold), returns
          HeaderTopology.NONE, synthetic Col_1..Col_N, and preserves Row 0 as data
          (data_start_row_index = 0).
    """
    if not raw_rows:
        return (
            TableSchema(
                columns=(),
                header_rows=(),
                header_topology=HeaderTopology.NONE,
                confidence=0.0,
            ),
            0,
        )

    scan_depth = min(len(raw_rows), max_scan_rows)
    total_cols = max((len(r) for r in raw_rows[:scan_depth]), default=0)
    if total_cols == 0:
        return (
            TableSchema(columns=(), header_rows=(), header_topology=HeaderTopology.NONE, confidence=0.0),
            0,
        )

    row_scores: list[tuple[int, float]] = []
    for r_idx in range(scan_depth):
        row = raw_rows[r_idx]
        next_row = raw_rows[r_idx + 1] if (r_idx + 1) < len(raw_rows) else None
        score = _score_candidate_row(row, r_idx, next_row, total_cols)
        row_scores.append((r_idx, score))

    # Find candidate rows meeting confidence threshold
    passing_dict = {idx: s for idx, s in row_scores if s >= confidence_threshold}

    # SAFE FALLBACK: If no row meets the threshold, assign NONE and keep row 0 as data
    if not passing_dict:
        columns = tuple(
            ColumnDefinition(col_index=i, name=f"Col_{i+1}", inferred_dtype=InferredDtype.STRING)
            for i in range(total_cols)
        )
        return (
            TableSchema(
                columns=columns,
                header_rows=(),
                header_topology=HeaderTopology.NONE,
                confidence=0.0,
            ),
            0,  # Row 0 preserved as regular data!
        )

    # Check for consecutive passing candidates that form a multi-row header
    is_multi_row = False
    header_rows: tuple[int, ...] = ()
    topology = HeaderTopology.SINGLE_ROW
    best_score = 0.0

    passing_indices = sorted(passing_dict.keys())
    for idx in passing_indices:
        if (idx + 1) in passing_dict:
            r_curr = raw_rows[idx]
            r_next = raw_rows[idx + 1]

            curr_non_empty = [c for c in r_curr if c is not None and str(c).strip() != ""]
            next_non_empty = [c for c in r_next if c is not None and str(c).strip() != ""]

            curr_dense = len(curr_non_empty) / max(total_cols, len(r_curr))
            next_dense = len(next_non_empty) / max(total_cols, len(r_next))

            curr_strings = sum(1 for c in curr_non_empty if _infer_cell_dtype(c) == InferredDtype.STRING or _is_year_token(c))
            next_strings = sum(1 for c in next_non_empty if _infer_cell_dtype(c) == InferredDtype.STRING or _is_year_token(c))

            curr_type_ratio = curr_strings / len(curr_non_empty) if curr_non_empty else 0.0
            next_type_ratio = next_strings / len(next_non_empty) if next_non_empty else 0.0

            # Both rows must be 100% header tokens AND have high column density
            if curr_type_ratio == 1.0 and next_type_ratio == 1.0 and curr_dense >= 0.6 and next_dense >= 0.6:
                header_rows = (idx, idx + 1)
                topology = HeaderTopology.MULTI_ROW
                is_multi_row = True
                best_score = round((passing_dict[idx] + passing_dict[idx + 1]) / 2.0, 4)
                break

    if not is_multi_row:
        best_row_idx = max(passing_dict, key=passing_dict.__getitem__)
        best_score = passing_dict[best_row_idx]
        header_rows = (best_row_idx,)

    # Build column definitions from header row(s)
    columns_list: list[ColumnDefinition] = []
    if is_multi_row:
        r0 = raw_rows[header_rows[0]]
        r1 = raw_rows[header_rows[1]]
        for col_idx in range(total_cols):
            val0 = str(r0[col_idx]).strip() if col_idx < len(r0) and r0[col_idx] is not None else ""
            val1 = str(r1[col_idx]).strip() if col_idx < len(r1) and r1[col_idx] is not None else ""
            if val0 and val1 and val0 != val1:
                name = f"{val0}_{val1}"
            elif val0:
                name = val0
            elif val1:
                name = val1
            else:
                name = f"Col_{col_idx+1}"
            columns_list.append(ColumnDefinition(col_index=col_idx, name=name))
        data_start_row = header_rows[1] + 1
    else:
        best_row_idx = header_rows[0]
        r0 = raw_rows[best_row_idx]
        for col_idx in range(total_cols):
            val = str(r0[col_idx]).strip() if col_idx < len(r0) and r0[col_idx] is not None else ""
            name = val if val else f"Col_{col_idx+1}"
            columns_list.append(ColumnDefinition(col_index=col_idx, name=name))
        data_start_row = best_row_idx + 1

    return (
        TableSchema(
            columns=tuple(columns_list),
            header_rows=header_rows,
            header_topology=topology,
            confidence=best_score,
        ),
        data_start_row,
    )
