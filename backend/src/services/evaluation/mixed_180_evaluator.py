"""Dedicated evaluator for the KRE mixed-corpus 180-question benchmark."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from services.evaluation.benchmark_scorer import content_match, extract_numbers_normalized

SUPPORTED_STATES = frozenset({"COMPLETE", "NO_SUPPORT", "PARTIAL", "CLARIFICATION_REQUIRED", "FAILED"})
SCORER_VERSION = "2.0.0"

PARTIAL_LIMITATION_KEYWORDS = (
    "partial",
    "partially",
    "incomplete",
    "missing",
    "limited to",
    "only covers",
    "only available",
    "data unavailable",
    "unavailable",
    "not available",
    "gap",
    "omits",
    "excluding",
    "does not include",
    "only provides",
    "only found",
    "cannot confirm",
    "not reported",
    "no data",
    "only for",
)


def _has_explicit_limitation_statement(ans: str) -> bool:
    low = (ans or "").strip().lower()
    return any(kw in low for kw in PARTIAL_LIMITATION_KEYWORDS)


def extract_signed_numeric_claims(text: str) -> list[dict[str, Any]]:
    """Extract numeric claims with signs, percentage flags, and negation status."""
    if not text:
        return []
    cleaned = re.sub(r"(?<=\d),(?=\d)", "", text)
    claims: list[dict[str, Any]] = []
    pattern = re.compile(
        r"(?P<prefix>\b(?:decrease|decreased|drop|dropped|fell|decline|declined|reduction|reduced|loss|down|increase|increased|rise|rose|growth|grew|gain|up)\b\s+(?:of\s+|by\s+)?)?"
        r"(?P<neg>(?<!\d)[-–—]|negative\s+|minus\s+)?"
        r"(?P<num>\b\d+(?:\.\d+)?)"
        r"(?P<pct>%|percent\b)?",
        re.IGNORECASE,
    )
    for m in pattern.finditer(cleaned):
        raw_num = m.group("num")
        try:
            val = float(raw_num)
        except ValueError:
            continue
        prefix = (m.group("prefix") or "").lower()
        neg_sign = bool(m.group("neg"))
        is_pct = bool(m.group("pct"))

        is_decrease = any(w in prefix for w in ("decrease", "drop", "fell", "decline", "reduction", "loss", "down"))
        is_increase = any(w in prefix for w in ("increase", "rise", "rose", "growth", "grew", "gain", "up"))

        if neg_sign or is_decrease:
            effective_sign = -1
        elif is_increase:
            effective_sign = 1
        else:
            effective_sign = 1

        start_idx = max(0, m.start() - 30)
        context = cleaned[start_idx:m.end()].lower()
        is_negated = bool(re.search(r"\b(?:not|no|never|neither|n't)\b", context))

        claims.append({
            "value": val,
            "sign": effective_sign,
            "is_percentage": is_pct,
            "is_negated": is_negated,
            "raw": m.group(0),
        })
    return claims


def verify_numeric_and_sign_agreement(pref_ans: str, ans: str) -> bool:
    pref_claims = extract_signed_numeric_claims(pref_ans)
    ans_claims = extract_signed_numeric_claims(ans)
    if not pref_claims:
        return True
    if not ans_claims:
        return False

    for p_claim in pref_claims:
        p_val = p_claim["value"]
        p_sign = p_claim["sign"]
        p_pct = p_claim["is_percentage"]

        matched = False
        for a_claim in ans_claims:
            a_val = a_claim["value"]
            a_sign = a_claim["sign"]
            a_pct = a_claim["is_percentage"]
            a_neg = a_claim["is_negated"]

            tol = 0.05 if (p_pct or p_val < 1.0) else (0.01 if float(p_val).is_integer() else 0.05)
            if abs(a_val - p_val) <= tol and a_pct == p_pct:
                if a_sign != p_sign:
                    return False
                if a_neg:
                    return False
                matched = True
                break
        if not matched:
            return False
    return True



def load_engine_requests(questions_only_path: Path) -> list[dict[str, str]]:
    """Load only sanitized {id, question} pairs for engine execution (no gold labels/hints)."""
    data = json.loads(questions_only_path.read_text(encoding="utf-8"))
    return [{"id": str(q["id"]), "question": str(q["question"])} for q in data.get("questions", [])]


def validate_180_dataset(
    questions_only_path: Path,
    benchmark_path: Path,
    documents_path: Path,
    staged_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate 180 IDs, question texts, states, source references, and optional staged file hashes."""
    q_only = json.loads(questions_only_path.read_text(encoding="utf-8"))
    bench = json.loads(benchmark_path.read_text(encoding="utf-8"))
    docs = json.loads(documents_path.read_text(encoding="utf-8"))

    qo_qs = q_only.get("questions", [])
    b_qs = bench.get("questions", [])
    manifest = docs.get("manifest", [])
    if len(qo_qs) != 180 or len(b_qs) != 180:
        raise ValueError(f"Expected 180 questions, got q_only={len(qo_qs)}, bench={len(b_qs)}")
    if [q["id"] for q in qo_qs] != [q["id"] for q in b_qs]:
        raise ValueError("Question ID order mismatch between kre_questions_only_180.json and kre_benchmark_180.json")
    if [q["question"] for q in qo_qs] != [q["question"] for q in b_qs]:
        raise ValueError("Question text mismatch between kre_questions_only_180.json and kre_benchmark_180.json")

    manifest_by_name = {m["filename"]: m for m in manifest}
    for bq in b_qs:
        st = bq.get("expected_state")
        if st not in SUPPORTED_STATES:
            raise ValueError(f"Unsupported expected_state '{st}' on {bq['id']}")
        for es in bq.get("expected_sources") or []:
            doc_name = es.get("document") if isinstance(es, dict) else es
            if doc_name not in manifest_by_name:
                raise ValueError(f"Unknown source '{doc_name}' referenced by {bq['id']}")

    source_checks: list[dict[str, Any]] = []
    if staged_dir is not None:
        for m in manifest:
            fn = m["filename"]
            p = staged_dir / fn
            if not p.exists():
                raise FileNotFoundError(f"Staged source missing: {p}")
            raw = p.read_bytes()
            sha = hashlib.sha256(raw).hexdigest()
            if sha != m["sha256"] or len(raw) != m["bytes"]:
                raise ValueError(f"Hash/size mismatch for {fn}: got ({sha}, {len(raw)}), expected ({m['sha256']}, {m['bytes']})")
            source_checks.append({"filename": fn, "sha256": sha, "bytes": len(raw)})

    return {
        "question_count": len(b_qs),
        "document_count": len(manifest),
        "source_checks": source_checks,
    }


def _cell_row_index(cell_ref: str) -> int | None:
    m = re.match(r"^[A-Za-z]+(\d+)$", (cell_ref or "").strip())
    return int(m.group(1)) if m else None


def resolve_180_locators(
    expected_sources: list[dict[str, Any]],
    chunks: list[Any],
    snapshot_by_filename: dict[str, Any],
) -> dict[str, Any]:
    """Resolve 180-benchmark locators against active-version chunks and snapshots.

    Returns dict with:
      - resolved_chunk_ids: set[str]
      - resolved_documents: set[str]
      - structured_targets: list[dict]
      - unscorable_reasons: list[str]
    """
    resolved_ids: set[str] = set()
    resolved_docs: set[str] = set()
    per_doc_chunk_ids: dict[str, set[str]] = {}
    structured_docs: set[str] = set()
    structured_targets: list[dict[str, Any]] = []
    unscorable_reasons: list[str] = []

    for es in expected_sources or []:
        doc_name = es.get("document")
        loc = es.get("locator") or {}
        if not doc_name or not isinstance(loc, dict) or not loc:
            unscorable_reasons.append(f"empty_or_malformed_locator:{es}")
            continue

        snap = snapshot_by_filename.get(doc_name)
        if snap is None:
            unscorable_reasons.append(f"missing_active_snapshot:{doc_name}")
            continue
        active_did = str(getattr(snap, "document_id", "") or "")
        active_ver = str(getattr(snap, "version", "") or "")
        doc_chunks = [
            c for c in chunks
            if str(getattr(c, "document_id", "") or "") == active_did
            and (not active_ver or not getattr(c, "document_version", None) or str(c.document_version) == active_ver)
        ]

        if "page" in loc:
            pg = int(loc["page"])
            hits = {
                str(c.id) for c in doc_chunks
                if getattr(c, "page_number", None) == pg
                or bool(re.search(rf"(?::|_)page:{pg}(?::|_|\b|$)", str(c.id), re.IGNORECASE))
                or f":page:{pg}:" in str(c.id)
            }
            if hits:
                resolved_ids.update(hits)
                resolved_docs.add(doc_name)
                per_doc_chunk_ids.setdefault(doc_name, set()).update(hits)
            else:
                unscorable_reasons.append(f"unresolved_pdf_page:{doc_name}:page={pg}")

        elif "slide" in loc:
            sl = int(loc["slide"])
            hits = {
                str(c.id) for c in doc_chunks
                if getattr(c, "page_number", None) == sl
                or bool(re.search(rf"(?::|_)slide:{sl}(?::|_|\b|$)", str(c.id), re.IGNORECASE))
                or bool(re.search(rf":s{sl}(?::|_|\b|$)", str(c.id), re.IGNORECASE))
                or bool(re.search(rf"\bslide\s+{sl}\b", str(getattr(c, "location_reference", "") or ""), re.IGNORECASE))
            }
            if hits:
                resolved_ids.update(hits)
                resolved_docs.add(doc_name)
                per_doc_chunk_ids.setdefault(doc_name, set()).update(hits)
            else:
                unscorable_reasons.append(f"unresolved_slide:{doc_name}:slide={sl}")

        elif "document_section_page" in loc:
            sp = int(loc["document_section_page"])
            hits = {
                str(c.id) for c in doc_chunks
                if getattr(c, "page_number", None) == sp
                or bool(re.search(rf":p:(?:{sp}|{max(0, sp - 1)})(?::|_|\b|$)", str(c.id)))
            }
            if hits:
                resolved_ids.update(hits)
                resolved_docs.add(doc_name)
                per_doc_chunk_ids.setdefault(doc_name, set()).update(hits)
            else:
                unscorable_reasons.append(f"unresolved_docx_section:{doc_name}:section={sp}")

        elif loc.get("operation") == "sheet_inventory":
            inv = getattr(snap, "sheet_inventory", None)
            if inv and getattr(inv, "complete", False):
                inv_id = f"{active_did}:sheet_inventory:{active_ver}"
                resolved_ids.add(inv_id)
                resolved_docs.add(doc_name)
                per_doc_chunk_ids.setdefault(doc_name, set()).add(inv_id)
            else:
                unscorable_reasons.append(f"missing_sheet_inventory:{doc_name}")

        elif "sheet" in loc and ("cell" in loc or "range" in loc or "operation" in loc):
            sheet = str(loc["sheet"])
            row_idx = _cell_row_index(loc.get("cell", ""))
            if row_idx is not None:
                # Row 1 in Excel is header; data rows start at row 2 (xls:Sheet1:row1 / row2)
                candidates = {
                    f"{active_did}:xls:{sheet}:row{row_idx}",
                    f"{active_did}:xls:{sheet}:row{max(1, row_idx - 1)}",
                }
                hits = {str(c.id) for c in doc_chunks if str(c.id) in candidates}
                if hits:
                    resolved_ids.update(hits)
                    resolved_docs.add(doc_name)
                    per_doc_chunk_ids.setdefault(doc_name, set()).update(hits)
                else:
                    unscorable_reasons.append(f"unresolved_cell:{doc_name}:{sheet}:{loc['cell']}")
            else:
                structured_targets.append({"document": doc_name, "document_id": active_did, "locator": loc})
                resolved_docs.add(doc_name)
                structured_docs.add(doc_name)

        elif any(k in loc for k in ("filter", "column", "columns", "operation", "selection")):
            structured_targets.append({"document": doc_name, "document_id": active_did, "locator": loc})
            resolved_docs.add(doc_name)
            structured_docs.add(doc_name)

        else:
            unscorable_reasons.append(f"unsupported_locator_type:{doc_name}:{sorted(loc.keys())}")

    return {
        "resolved_chunk_ids": resolved_ids,
        "resolved_documents": resolved_docs,
        "per_doc_chunk_ids": per_doc_chunk_ids,
        "structured_documents": structured_docs,
        "structured_targets": structured_targets,
        "unscorable_reasons": unscorable_reasons,
    }


def _is_refusal_text(ans: str) -> bool:
    low = (ans or "").strip().lower()
    return low == "not_found" or any(
        p in low
        for p in (
            "couldn't find",
            "could not find",
            "no relevant",
            "does not contain",
            "not mentioned",
            "not available",
            "insufficient",
            "cannot determine",
        )
    )


def _is_clarification_text(ans: str) -> bool:
    low = (ans or "").strip().lower()
    return any(p in low for p in ("clarif", "which ", "please specify", "could you clarify", "ambiguous"))


def grade_180_record(
    bench_item: dict[str, Any],
    response: dict[str, Any],
    resolved: dict[str, Any],
) -> dict[str, Any]:
    """Grade one 180-benchmark query response against expected_state and resolved locators."""
    exp_state = bench_item.get("expected_state", "COMPLETE")
    pref_ans = str(bench_item.get("preferred_answer") or "")
    ans_raw = response.get("answer", "")
    ans = json.dumps(ans_raw) if isinstance(ans_raw, (dict, list)) else str(ans_raw or "")
    citations = response.get("citations") or []
    cited_ids = {str(c.get("chunk_id")) for c in citations if isinstance(c, dict) and c.get("chunk_id")}
    cited_docs = {str(c.get("document_filename") or "") for c in citations if isinstance(c, dict) and c.get("document_filename")}
    executed_path = str(response.get("executed_path") or "")

    unscorable = list(resolved.get("unscorable_reasons") or [])
    if unscorable and exp_state in ("COMPLETE", "PARTIAL"):
        return {
            "id": bench_item["id"],
            "expected_state": exp_state,
            "scorable": False,
            "unscorable_reasons": unscorable,
            "answer_correct": False,
            "grounded_correct": False,
            "reason": f"unscorable:{unscorable[0]}",
        }

    is_ref = _is_refusal_text(ans)
    is_clar = _is_clarification_text(ans)

    if exp_state in ("NO_SUPPORT", "FAILED"):
        ans_ok = is_ref
        grd_ok = is_ref and len(citations) == 0
        reason = "verified_refusal" if grd_ok else ("refusal_with_citations" if ans_ok else "false_answer_on_no_support")
        return {
            "id": bench_item["id"],
            "expected_state": exp_state,
            "scorable": True,
            "unscorable_reasons": [],
            "answer_correct": ans_ok,
            "grounded_correct": grd_ok,
            "reason": reason,
        }

    if exp_state == "CLARIFICATION_REQUIRED":
        ans_ok = is_clar and not is_ref
        grd_ok = ans_ok
        return {
            "id": bench_item["id"],
            "expected_state": exp_state,
            "scorable": True,
            "unscorable_reasons": [],
            "answer_correct": ans_ok,
            "grounded_correct": grd_ok,
            "reason": "clarification_verified" if grd_ok else "missing_clarification",
        }

    # COMPLETE or PARTIAL
    if not ans.strip() or (is_ref and (exp_state != "PARTIAL" or not citations)):
        return {
            "id": bench_item["id"],
            "expected_state": exp_state,
            "scorable": True,
            "unscorable_reasons": [],
            "answer_correct": False,
            "grounded_correct": False,
            "reason": "unexpected_refusal",
        }

    pref_nums = extract_numbers_normalized(pref_ans)
    ans_nums = extract_numbers_normalized(ans)
    numeric_signs_ok = verify_numeric_and_sign_agreement(pref_ans, ans)
    if pref_nums:
        extra_conflict_nums = set() if exp_state == "PARTIAL" else (ans_nums - pref_nums)
        ans_ok = (pref_nums <= ans_nums) and not extra_conflict_nums and bool(content_match(ans, pref_ans)) and numeric_signs_ok
    else:
        ans_ok = bool(content_match(ans, pref_ans)) and numeric_signs_ok

    rel_ids = set(resolved.get("resolved_chunk_ids") or set())
    rel_docs = set(resolved.get("resolved_documents") or set())
    per_doc_ids: dict[str, set[str]] = dict(resolved.get("per_doc_chunk_ids") or {})
    struct_docs = set(resolved.get("structured_documents") or set())
    has_struct = bool(resolved.get("structured_targets"))

    if len(rel_docs) > 1 and bench_item.get("expected_source_mode") != "any":
        # Multi-source questions require every expected source to be backed by a valid citation
        cit_ok = bool(citations)
        for doc_name in rel_docs:
            doc_ids = per_doc_ids.get(doc_name, set())
            if doc_ids and (cited_ids & doc_ids):
                continue
            if doc_name in struct_docs and executed_path in ("structured_aggregate", "deterministic_math", "deterministic_sheet_inventory") and (citations and doc_name in cited_docs):
                continue
            cit_ok = False
            break
    elif rel_ids and (cited_ids & rel_ids):
        cit_ok = True
    elif has_struct and executed_path in ("structured_aggregate", "deterministic_math", "deterministic_sheet_inventory"):
        if not citations:
            cit_ok = False
        else:
            cit_ok = bool(rel_docs) and bool(cited_docs & rel_docs) and (cited_docs <= rel_docs)
    else:
        cit_ok = False

    # Enforce field and row precision when specified
    if cit_ok and citations and has_struct:
        for struct_t in resolved.get("structured_targets") or []:
            loc = struct_t.get("locator") or {}
            exp_cols: list[str] = []
            if "column" in loc:
                exp_cols.append(str(loc["column"]).lower())
            if "columns" in loc:
                exp_cols.extend([str(c).lower() for c in loc["columns"]])
            if exp_cols:
                for cit in citations:
                    cited_col = str(cit.get("column") or cit.get("field") or cit.get("target_column") or "").lower()
                    if cited_col and not any(ec == cited_col or ec in cited_col or cited_col in ec for ec in exp_cols):
                        cit_ok = False
                        break
            exp_cell = loc.get("cell")
            if exp_cell and cit_ok:
                exp_row = _cell_row_index(exp_cell)
                for cit in citations:
                    c_id = str(cit.get("chunk_id") or "")
                    c_loc = str(cit.get("location_reference") or "")
                    if exp_row is not None:
                        row_m = re.findall(r"row\s*(\d+)", f"{c_id} {c_loc}".lower())
                        if row_m and not any(int(r) in (exp_row, max(1, exp_row - 1)) for r in row_m):
                            cit_ok = False
                            break

    if exp_state == "PARTIAL":
        # Partial expects honest acknowledgment of missing/unavailable parts or partial answer
        has_limit = _has_explicit_limitation_statement(ans)
        grd_ok = bool(citations) and cit_ok and ans_ok and has_limit
        if not has_limit and ans_ok and cit_ok:
            reason = "missing_partial_limitation"
        else:
            reason = "grounded_and_verified" if grd_ok else ("citation_locator_mismatch" if ans_ok else "answer_mismatch")
    else:
        grd_ok = ans_ok and cit_ok
        reason = "grounded_and_verified" if grd_ok else ("citation_locator_mismatch" if ans_ok else "answer_mismatch")

    return {
        "id": bench_item["id"],
        "expected_state": exp_state,
        "scorable": True,
        "unscorable_reasons": [],
        "answer_correct": ans_ok,
        "grounded_correct": grd_ok,
        "reason": reason,
    }
