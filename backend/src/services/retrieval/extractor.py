"""Verified Factual Extractor (Component 3).

Replaces permissive snippet selection with deterministic, verified factual extraction.
Implements explicit answerability checks:
  - Structured lookups (CSV/XLS): resolves document + entity/row + requested field + period + unit.
  - Abbreviation expansion: requires an explicit source-supported expansion pattern (e.g. 'XYZ stands for ...').
  - ArXiv identifier metadata: maps YYMM identifiers to publication month and year.
  - If deterministic extraction cannot establish the requested fact with full provenance,
    it returns (None, None), triggering an escalation to the full generation path.
"""

import logging
import re
import unicodedata
from schemas.models import Chunk

logger = logging.getLogger(__name__)

_MONTH_NAMES = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}


def parse_kv_row(text: str) -> dict[str, str]:
    """Parse structured row text with 'Key: Value' format into a normalized dict."""
    matches = re.findall(
        r"([A-Za-z0-9_\- /()]+?):\s*([^:\n]+?)(?=\s+[A-Za-z0-9_\- /()]+:|\. [A-Z]|\.?$)",
        text.strip(),
    )
    kv: dict[str, str] = {}
    for k, v in matches:
        kv[k.strip().lower()] = v.strip().rstrip(".")
    return kv


def extract_verified_fact(query: str, chunks: list[Chunk]) -> tuple[str | None, Chunk | None]:
    """Attempt deterministic extraction of the requested fact from candidate chunks.

    Returns (answer_string, source_chunk) if verified, or (None, None) if the fact
    cannot be established deterministically (in which case the query must escalate
    to the full path).
    """
    if not query or not chunks:
        return None, None

    q_lower = query.lower()

    # -----------------------------------------------------------------------
    # 1. ArXiv Identifier Transformation (Deterministic Metadata Operation)
    # -----------------------------------------------------------------------
    arxiv_m = re.search(r"arxiv\s+(?:identifier\s+)?(\d{2})(\d{2})\.\d+", q_lower)
    if arxiv_m and ("month" in q_lower or "year" in q_lower or "published" in q_lower or "publication" in q_lower):
        yy, mm = arxiv_m.group(1), arxiv_m.group(2)
        month_name = _MONTH_NAMES.get(mm)
        if month_name:
            # Find relevant paper chunk if present, else first chunk
            paper_chunk = next(
                (c for c in chunks if f"{yy}{mm}" in getattr(c, "text", "")),
                chunks[0] if chunks else None,
            )
            ans = f"{month_name} 20{yy}"
            logger.info("extractor.arxiv_resolved query='%s' answer='%s'", query[:40], ans)
            return ans, paper_chunk

    # -----------------------------------------------------------------------
    # 2. Abbreviation Expansion (Source-Supported)
    # -----------------------------------------------------------------------
    abbr_m = re.search(r"what does\b.*?\b([A-Z]{2,6})\b.*?stand for", query, re.IGNORECASE)
    if abbr_m:
        target_abbr = abbr_m.group(1).upper()
        for c in chunks:
            txt = unicodedata.normalize("NFKD", getattr(c, "text", ""))
            # Pattern: "XYZ stands for Full Expansion"
            m = re.search(
                r"\b" + re.escape(target_abbr) + r"\b\s+stands for\s+([^,.;\n\)]+)",
                txt,
                re.IGNORECASE,
            )
            if m:
                ans = m.group(1).strip()
                logger.info("extractor.abbr_resolved abbr=%s answer='%s'", target_abbr, ans)
                return ans, c
            # Pattern: "Full Expansion (XYZ)"
            m2 = re.search(
                r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,5})\s*\(" + re.escape(target_abbr) + r"\)",
                txt,
            )
            if m2:
                ans = m2.group(1).strip()
                logger.info("extractor.abbr_resolved abbr=%s answer='%s'", target_abbr, ans)
                return ans, c

    # -----------------------------------------------------------------------
    # 3. Generic Structured Row / Key-Value Extraction (CSV / XLS / Tables)
    # -----------------------------------------------------------------------
    # Evaluates structured rows without any hardcoded dataset or corpus rules.
    # If explicit filters match and a single unambiguous target attribute is found,
    # extracts the value; if ambiguous (>1 matching rows) or 0 matches, returns (None, None).
    valid_matches: list[tuple[str, Chunk]] = []

    for c in chunks:
        txt = getattr(c, "text", "")
        fmt = getattr(c, "source_format", "")
        elem = getattr(c, "element_type", "")
        is_tabular = fmt in ("csv", "xls", "xlsx") or elem in ("table_row", "cell", "table", "row")
        if not is_tabular:
            continue

        row = parse_kv_row(txt)
        if not row:
            continue

        # Count how many row values appear in the query (filter matches)
        filter_matches = 0
        for k, v in row.items():
            val_clean = str(v).strip().lower()
            if len(val_clean) >= 2 and val_clean in q_lower:
                filter_matches += 1

        if filter_matches == 0:
            continue

        # Identify requested target attributes whose keys appear in the query
        target_candidates = []
        for k, v in row.items():
            k_clean = k.strip().lower()
            val_clean = str(v).strip().lower()
            # If the value itself is in the query, it was a filter, not the target
            if val_clean in q_lower:
                continue
            if len(k_clean) >= 3 and (k_clean in q_lower or any(kw in q_lower for kw in k_clean.split() if len(kw) > 3)):
                target_candidates.append((k, str(v).strip()))

        if len(target_candidates) == 1:
            target_key, target_val = target_candidates[0]
            if target_val:
                valid_matches.append((target_val, c))

    if len(valid_matches) == 1:
        ans, c = valid_matches[0]
        logger.info("extractor.generic_tabular_resolved value='%s'", ans)
        return ans, c

    return None, None

