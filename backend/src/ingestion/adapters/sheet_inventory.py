"""Extract version-bound sheet inventory from XLS and XLSX workbooks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_NUMERIC_VALUE_RE = re.compile(r"^[-+]?[$€£¥₹]?\s*\d[\d,]*(?:\.\d+)?%?$")


def _is_meaningful_data_cell(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return False
    if isinstance(val, (int, float)):
        return True
    s = str(val).strip()
    if not s:
        return False
    return bool(_NUMERIC_VALUE_RE.fullmatch(s))


def _is_header_label_cell(val: Any) -> bool:
    if not isinstance(val, str):
        return False
    s = val.strip()
    if not s or len(s) > 60:
        return False
    if _NUMERIC_VALUE_RE.fullmatch(s):
        return False
    if any(p in s for p in (". ", "? ", "! ")) or s.endswith((".", "?", "!")):
        return False
    words = s.split()
    return 1 <= len(words) <= 6


def _classify_single_row(values: list[Any]) -> tuple[str, bool, int]:
    """Classify a sheet whose only non-blank content is on the first row.

    Returns (status, header_only, data_rows):
      - Numeric/value cell present -> ('populated', False, 1)
      - Verified short header labels only -> ('empty', True, 0)
      - Ambiguous non-blank first row -> ('unknown', False, 0)
    """
    non_blank = [v for v in values if v is not None and str(v).strip() != ""]
    if not non_blank:
        return "empty", False, 0
    if any(_is_meaningful_data_cell(v) for v in non_blank):
        return "populated", False, 1
    if len(non_blank) >= 1 and all(_is_header_label_cell(v) for v in non_blank):
        # Single-cell prose or long text already failed _is_header_label_cell;
        # require >= 2 columns or <= 3 short label words for verified header-only.
        if len(non_blank) >= 2 or len(str(non_blank[0]).split()) <= 3:
            return "empty", True, 0
    return "unknown", False, 0


def extract_xls_sheet_inventory(path: Path) -> list[dict[str, Any]]:
    """Extract sheet inventory from XLS via xlrd, distinguishing header-only vs first-row data."""
    import xlrd

    workbook = xlrd.open_workbook(str(path))
    entries: list[dict[str, Any]] = []
    for sheet in workbook.sheets():
        if sheet.nrows == 0 or sheet.ncols == 0:
            entries.append(
                {
                    "name": str(sheet.name),
                    "status": "empty",
                    "data_rows": 0,
                    "header_only": False,
                }
            )
            continue

        non_blank_rows: list[tuple[int, list[Any]]] = []
        for r_idx in range(sheet.nrows):
            vals = sheet.row_values(r_idx)
            if any(v is not None and str(v).strip() != "" for v in vals):
                non_blank_rows.append((r_idx, vals))

        if not non_blank_rows:
            status, header_only, data_rows = "empty", False, 0
        elif len(non_blank_rows) == 1 and non_blank_rows[0][0] == 0:
            status, header_only, data_rows = _classify_single_row(non_blank_rows[0][1])
        else:
            # 2+ non-blank rows, or non-blank data below row 0
            data_rows = sum(1 for r_idx, _ in non_blank_rows if r_idx >= 1)
            if data_rows == 0:
                data_rows = len(non_blank_rows)
            status, header_only = "populated", False

        entries.append(
            {
                "name": str(sheet.name),
                "status": status,
                "data_rows": data_rows,
                "header_only": header_only,
            }
        )
    return entries


def extract_xlsx_sheet_inventory(path: Path) -> list[dict[str, Any]]:
    """Extract sheet inventory from XLSX using openpyxl read-only iteration."""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    entries: list[dict[str, Any]] = []
    try:
        for sheet in wb.worksheets:
            row1_vals: list[Any] | None = None
            data_rows_below_1 = 0
            for r_idx, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if any(v is not None and str(v).strip() != "" for v in row):
                    if r_idx == 1:
                        row1_vals = list(row)
                    else:
                        data_rows_below_1 += 1

            if row1_vals is None and data_rows_below_1 == 0:
                status, header_only, data_rows = "empty", False, 0
            elif row1_vals is not None and data_rows_below_1 == 0:
                status, header_only, data_rows = _classify_single_row(row1_vals)
            else:
                status = "populated"
                header_only = False
                data_rows = data_rows_below_1

            entries.append(
                {
                    "name": str(sheet.title),
                    "status": status,
                    "data_rows": data_rows,
                    "header_only": header_only,
                }
            )
    finally:
        wb.close()
    return entries


def extract_workbook_sheet_inventory(
    path: Path,
    source_format: str,
    version: str | None,
    incomplete_coverage: bool = False,
) -> dict[str, Any] | None:
    """Build a version-bound sheet inventory dict for xls/xlsx workbooks."""
    fmt = str(source_format or "").lower()
    if fmt not in ("xls", "xlsx") or not version:
        return None
    if fmt == "xls":
        sheets = extract_xls_sheet_inventory(path)
    else:
        sheets = extract_xlsx_sheet_inventory(path)
    return {
        "version": str(version),
        "complete": not bool(incomplete_coverage),
        "sheets": sheets,
    }
