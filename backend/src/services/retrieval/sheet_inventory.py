"""Version-bound workbook sheet inventory resolution and evidence construction."""

from __future__ import annotations

import json
import re
from typing import Any

from schemas.models import Chunk
from services.retrieval.evidence_contract import EvidenceItem, evidence_item_from_chunk

_EMPTY_OR_ABSENT_SHEET_RE = re.compile(
    r"\b(sheet\s*[a-z0-9_]+(?:\s*(?:,|and)\s*sheet\s*[a-z0-9_]+)*)\s+(?:is\s+|are\s+)?(?:empty|blank|have\s+0\s+data\s+rows|has\s+0\s+data\s+rows|have\s+no\s+data|has\s+no\s+data|contain\s+no\s+data|contains\s+no\s+data|absent|do\s+not\s+exist|does\s+not\s+exist|missing)\b",
    re.IGNORECASE,
)
_JSON_SHEET_STATUS_RE = re.compile(
    r'"(sheet\s*[a-z0-9_]+)"\s*:\s*"?(empty|blank|absent|missing|0)"?',
    re.IGNORECASE,
)
_SHEET_NAME_RE = re.compile(r"\bsheet\s*[a-z0-9_]+\b", re.IGNORECASE)
_SHEET_STATUS_QUERY_RE = re.compile(
    r"\b(?:populated|empty|blank|absent|missing|exist|status|sheets?|datasets?)\b",
    re.IGNORECASE,
)


def bind_workbook_sheet_query(
    query: str,
    retained_evidence_items: list[Any] | None = None,
) -> dict[str, Any]:
    """Bind workbook sheet-status intent and requested sheet names from query and retained evidence."""
    q = str(query or "")
    requested_sheets: list[str] = []
    seen: set[str] = set()
    for m in _SHEET_NAME_RE.finditer(q):
        norm = re.sub(r"\s+", "", m.group(0)).lower()
        if norm not in seen:
            seen.add(norm)
            requested_sheets.append(re.sub(r"\s+", "", m.group(0)))

    inv_doc_ids: set[str] = set()
    for ev in retained_evidence_items or []:
        prov = getattr(ev, "provenance", {}) or {}
        meta = prov.get("metadata") if isinstance(prov, dict) else {}
        if getattr(ev, "strategy", "") == "sheet_inventory" or (
            isinstance(meta, dict) and meta.get("sheet_inventory")
        ):
            did = str(getattr(ev, "document_id", "") or "")
            if did:
                inv_doc_ids.add(did)

    is_active = bool(requested_sheets and _SHEET_STATUS_QUERY_RE.search(q) and inv_doc_ids)
    return {
        "is_active": is_active,
        "requested_sheets": requested_sheets,
        "inv_doc_ids": inv_doc_ids,
    }


def _is_snapshot_authorized(
    snapshot: Any,
    *,
    workspace_id: str | None = None,
    boundary_repo: Any | None = None,
    allowed_document_ids: set[str] | None = None,
) -> tuple[bool, str | None]:
    if snapshot is None:
        return False, "missing_snapshot"
    snap_ws = str(getattr(snapshot, "workspace_id", "") or "")
    snap_did = str(getattr(snapshot, "document_id", "") or "")
    if workspace_id is not None and snap_ws != str(workspace_id):
        return False, "wrong_workspace"
    if allowed_document_ids is not None and snap_did not in allowed_document_ids:
        return False, "out_of_scope"
    if boundary_repo is not None and snap_ws and hasattr(boundary_repo, "has_tombstone"):
        try:
            if boundary_repo.has_tombstone(snap_ws, "workspace", snap_ws):
                return False, "workspace_tombstoned"
            if snap_did and boundary_repo.has_tombstone(snap_ws, "source", snap_did):
                return False, "source_tombstoned"
        except Exception:
            return False, "tombstone_check_error"
    return True, None


def lookup_sheet_status(
    snapshot: Any,
    sheet_name: str,
    *,
    workspace_id: str | None = None,
    boundary_repo: Any | None = None,
    allowed_document_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Resolve sheet status ('populated', 'empty', 'absent', or 'unknown') from authoritative snapshot inventory."""
    ok, reason = _is_snapshot_authorized(
        snapshot,
        workspace_id=workspace_id,
        boundary_repo=boundary_repo,
        allowed_document_ids=allowed_document_ids,
    )
    if not ok:
        return {"status": "unknown", "sheet_name": sheet_name, "reason": reason}

    if bool(getattr(snapshot, "incomplete_coverage_flag", False)):
        return {"status": "unknown", "sheet_name": sheet_name, "reason": "incomplete_coverage"}

    inv = getattr(snapshot, "sheet_inventory", None)
    if inv is None:
        return {"status": "unknown", "sheet_name": sheet_name, "reason": "missing_inventory"}

    inv_complete = inv.get("complete") if isinstance(inv, dict) else getattr(inv, "complete", False)
    inv_version = inv.get("version") if isinstance(inv, dict) else getattr(inv, "version", None)
    snap_version = getattr(snapshot, "version", None)

    if not inv_complete:
        return {"status": "unknown", "sheet_name": sheet_name, "reason": "incomplete_inventory"}
    if not snap_version or str(inv_version) != str(snap_version):
        return {"status": "unknown", "sheet_name": sheet_name, "reason": "version_mismatch"}

    sheets = inv.get("sheets", ()) if isinstance(inv, dict) else getattr(inv, "sheets", ())
    target_norm = str(sheet_name or "").strip().lower()
    for entry in sheets:
        e_name = entry.get("name", "") if isinstance(entry, dict) else getattr(entry, "name", "")
        if str(e_name).strip().lower() == target_norm:
            e_status = entry.get("status", "unknown") if isinstance(entry, dict) else getattr(entry, "status", "unknown")
            e_rows = entry.get("data_rows", 0) if isinstance(entry, dict) else getattr(entry, "data_rows", 0)
            e_header_only = entry.get("header_only", False) if isinstance(entry, dict) else getattr(entry, "header_only", False)
            return {
                "status": str(e_status),
                "sheet_name": str(e_name),
                "data_rows": int(e_rows),
                "header_only": bool(e_header_only),
                "version": str(snap_version),
                "document_id": str(getattr(snapshot, "document_id", "")),
                "document_filename": str(getattr(snapshot, "source", "")),
            }

    return {
        "status": "absent",
        "sheet_name": sheet_name,
        "data_rows": 0,
        "header_only": False,
        "version": str(snap_version),
        "document_id": str(getattr(snapshot, "document_id", "")),
        "document_filename": str(getattr(snapshot, "source", "")),
    }


def build_sheet_inventory_chunk_and_evidence(
    snapshot: Any,
    *,
    workspace_id: str,
    boundary_repo: Any | None = None,
    allowed_document_ids: set[str] | None = None,
) -> tuple[Chunk, EvidenceItem] | None:
    """Build a version-bound Chunk and EvidenceItem representing a workbook's complete sheet inventory."""
    ok, _ = _is_snapshot_authorized(
        snapshot,
        workspace_id=workspace_id,
        boundary_repo=boundary_repo,
        allowed_document_ids=allowed_document_ids,
    )
    if not ok:
        return None
    if bool(getattr(snapshot, "incomplete_coverage_flag", False)):
        return None

    inv = getattr(snapshot, "sheet_inventory", None)
    if inv is None:
        return None
    inv_complete = inv.get("complete") if isinstance(inv, dict) else getattr(inv, "complete", False)
    inv_version = inv.get("version") if isinstance(inv, dict) else getattr(inv, "version", None)
    snap_version = str(getattr(snapshot, "version", "") or "")
    if not inv_complete or not snap_version or str(inv_version) != snap_version:
        return None

    inv_dict = inv if isinstance(inv, dict) else inv.model_dump()
    sheets = inv_dict.get("sheets") or []
    sheet_parts = []
    for entry in sheets:
        name = entry.get("name", "")
        status = entry.get("status", "unknown")
        data_rows = int(entry.get("data_rows", 0))
        if status == "empty" and entry.get("header_only"):
            sheet_parts.append(f"{name}: empty (0 data rows, header only)")
        elif status == "empty":
            sheet_parts.append(f"{name}: empty (0 data rows)")
        else:
            sheet_parts.append(f"{name}: {status} ({data_rows} data rows)")

    doc_id = str(getattr(snapshot, "document_id", ""))
    doc_source = str(getattr(snapshot, "source", ""))
    doc_format = str(getattr(snapshot, "format", "") or "xls")
    chunk_id = f"{doc_id}:sheet_inventory:{snap_version}"
    text = (
        f"Workbook {doc_source} sheet inventory (complete, version {snap_version}): "
        + "; ".join(sheet_parts)
        + "."
    )
    chunk = Chunk(
        id=chunk_id,
        document_id=doc_id,
        source_format=doc_format,
        text=text,
        element_type="sheet_inventory",
        location_reference="Sheet Inventory",
        workspace_id=workspace_id,
        version=snap_version,
        document_version=snap_version,
        metadata={
            "document_filename": doc_source,
            "document_version": snap_version,
            "version": snap_version,
            "sheet_inventory": inv_dict,
        },
    )
    ev = evidence_item_from_chunk(
        chunk=chunk,
        workspace_id=workspace_id,
        strategy="sheet_inventory",
        score=1.0,
        document_filename=doc_source,
        boundary_repo=boundary_repo,
        document_version=snap_version,
    )
    return chunk, ev


def verify_sheet_status_claims(
    answer: Any,
    retained_evidence_items: list[Any],
    citations: list[dict[str, Any]] | None = None,
    query: str = "",
) -> tuple[bool, str | None]:
    """Verify sheet-status claims against retained sheet_inventory and populated-sheet row evidence."""
    if not answer or answer == "NOT_FOUND":
        return True, None
    ans_str = answer if isinstance(answer, str) else json.dumps(answer)

    cited_ids = {
        str(c.get("chunk_id") or "")
        for c in (citations or [])
        if isinstance(c, dict) and c.get("chunk_id")
    }
    inv_items = []
    cited_ev_items = []
    for ev in retained_evidence_items or []:
        ev_id = str(getattr(ev, "evidence_id", ""))
        if ev_id not in cited_ids:
            continue
        cited_ev_items.append(ev)
        prov = getattr(ev, "provenance", {}) or {}
        meta = prov.get("metadata") if isinstance(prov, dict) else {}
        if getattr(ev, "strategy", "") == "sheet_inventory" or (
            isinstance(meta, dict) and meta.get("sheet_inventory")
        ):
            inv_items.append(ev)

    binding = bind_workbook_sheet_query(query, retained_evidence_items)
    matches = list(_EMPTY_OR_ABSENT_SHEET_RE.finditer(ans_str))
    json_matches = list(_JSON_SHEET_STATUS_RE.finditer(ans_str))
    if not matches and not json_matches and not inv_items and not binding["is_active"]:
        return True, None

    if not cited_ids:
        return False, "missing_citations_for_sheet_claim"

    if not inv_items:
        return False, "missing_authoritative_sheet_inventory"

    inv_doc_ids = {str(getattr(ev, "document_id", "") or "") for ev in inv_items}

    # Reject unrelated non-workbook citations on single-workbook sheet-inventory answers
    for ev in cited_ev_items:
        ev_did = str(getattr(ev, "document_id", "") or "")
        if ev_did and ev_did not in inv_doc_ids:
            return False, f"unrelated_document_citation:{ev_did}"

    claim_pairs: list[tuple[str, bool]] = []
    for m in matches:
        clause = m.group(0).lower()
        expects_absent = any(k in clause for k in ("absent", "does not exist", "do not exist", "missing"))
        for raw_name in re.findall(r"sheet\s*[a-z0-9_]+", m.group(1), re.IGNORECASE):
            claim_pairs.append((raw_name, expects_absent))
    for jm in json_matches:
        expects_absent = jm.group(2).lower() in ("absent", "missing")
        claim_pairs.append((jm.group(1), expects_absent))

    for raw_name, expects_absent in claim_pairs:
        claimed_sheet = re.sub(r"\s+", "", raw_name).lower()
        supported = False
        for ev in inv_items:
            inv = ((getattr(ev, "provenance", {}) or {}).get("metadata") or {}).get("sheet_inventory") or {}
            if not inv.get("complete"):
                continue
            sheets = inv.get("sheets") or []
            by_name = {re.sub(r"\s+", "", str(s.get("name", ""))).lower(): s for s in sheets}
            entry = by_name.get(claimed_sheet)
            if expects_absent and entry is None:
                supported = True
                break
            if not expects_absent and entry is not None and entry.get("status") == "empty":
                supported = True
                break
        if not supported:
            return False, f"unsupported_sheet_status_claim:{raw_name}"

    # If the answer references a populated sheet from the inventory, require a cited row/cell chunk
    # from that same workbook and matching sheet (reject wrong-sheet row citations).
    for ev in inv_items:
        ev_did = str(getattr(ev, "document_id", "") or "")
        inv = ((getattr(ev, "provenance", {}) or {}).get("metadata") or {}).get("sheet_inventory") or {}
        populated_names = [
            str(s.get("name", ""))
            for s in (inv.get("sheets") or [])
            if s.get("status") == "populated" and s.get("name")
        ]
        pop_norm = {re.sub(r"\s+", "", n).lower() for n in populated_names}
        if any(re.search(rf"\b{re.escape(name)}\b", ans_str, re.IGNORECASE) for name in populated_names):
            has_row_cit = False
            for c_ev in cited_ev_items:
                if str(getattr(c_ev, "document_id", "") or "") != ev_did or c_ev in inv_items:
                    continue
                c_meta = ((getattr(c_ev, "provenance", {}) or {}).get("metadata") or {})
                c_sheet = re.sub(r"\s+", "", str(c_meta.get("sheet", ""))).lower() if isinstance(c_meta, dict) and c_meta.get("sheet") else ""
                if c_sheet and c_sheet not in pop_norm:
                    continue
                has_row_cit = True
                break
            if not has_row_cit:
                return False, "missing_populated_sheet_row_support"

    return True, None


def resolve_workbook_sheet_status_answer(
    query: str,
    chunks: list[Any],
    *,
    workspace_id: str,
    boundary_repo: Any | None = None,
) -> dict[str, Any] | None:
    """Deterministically render a grounded sheet-status answer for one unambiguous workbook."""
    q = str(query or "")
    q_lower = q.lower()
    if not _SHEET_NAME_RE.search(q) or not _SHEET_STATUS_QUERY_RE.search(q):
        return None
    if re.search(r"\b(?:pdf|paper|10-q|strategy|compare\s+with)\b", q_lower):
        return None

    inv_chunks = [c for c in chunks if getattr(c, "element_type", "") == "sheet_inventory"]
    if len(inv_chunks) != 1:
        return None
    inv_chunk = inv_chunks[0]
    inv_did = str(getattr(inv_chunk, "document_id", "") or "")
    if not inv_did:
        return None

    if boundary_repo is None and workspace_id:
        try:
            from db.workspace_boundary_repo import get_workspace_boundary_repo
            boundary_repo = get_workspace_boundary_repo()
        except Exception:
            boundary_repo = None
    if boundary_repo is None:
        return None

    try:
        if boundary_repo.has_tombstone(workspace_id, "workspace", workspace_id):
            return None
        if boundary_repo.has_tombstone(workspace_id, "source", inv_did):
            return None
        snap = boundary_repo.get_document_snapshot(workspace_id, inv_did)
    except Exception:
        return None

    if snap is None or bool(getattr(snap, "incomplete_coverage_flag", False)):
        return None
    snap_inv = getattr(snap, "sheet_inventory", None)
    if snap_inv is None or not getattr(snap_inv, "complete", False):
        return None
    if str(getattr(snap_inv, "version", "")) != str(getattr(snap, "version", "")):
        return None

    requested: list[str] = []
    seen_req: set[str] = set()
    for m in _SHEET_NAME_RE.finditer(q):
        raw = re.sub(r"\s+", "", m.group(0))
        if raw.lower() not in seen_req:
            seen_req.add(raw.lower())
            requested.append(raw)
    if not requested:
        return None

    req_statuses: list[dict[str, Any]] = []
    for s_name in requested:
        st = lookup_sheet_status(snap, s_name, workspace_id=workspace_id, boundary_repo=boundary_repo)
        if st.get("status") == "unknown":
            return None
        req_statuses.append(st)

    populated_entries = [s for s in snap_inv.sheets if s.status == "populated"]
    pop_names_norm = {re.sub(r"\s+", "", s.name).lower() for s in populated_entries}

    row_chunk = None
    if populated_entries:
        for c in chunks:
            if str(getattr(c, "document_id", "") or "") != inv_did:
                continue
            if getattr(c, "element_type", "") == "sheet_inventory":
                continue
            c_meta = getattr(c, "metadata", None) or {}
            c_sheet = re.sub(r"\s+", "", str(c_meta.get("sheet", ""))).lower() if c_meta.get("sheet") else ""
            if c_sheet and c_sheet not in pop_names_norm:
                continue
            c_ver = getattr(c, "document_version", None) or getattr(c, "version", None) or c_meta.get("document_version") or c_meta.get("version")
            if c_ver and str(c_ver) != str(snap.version):
                continue
            row_chunk = c
            break
        if row_chunk is None:
            return None

    status_phrases = []
    for st in req_statuses:
        s_label = st["sheet_name"]
        s_val = st["status"]
        if s_val == "empty":
            status_phrases.append(f"{s_label} is empty (0 data rows)")
        elif s_val == "absent":
            status_phrases.append(f"{s_label} is absent")
        else:
            status_phrases.append(f"{s_label} is populated ({st['data_rows']} data rows)")

    all_non_pop = all(st["status"] in ("empty", "absent") for st in req_statuses)
    prefix = "No. " if (re.match(r"^\s*(?:are|is|do|does)\b", q_lower) and all_non_pop) else ""
    pop_summary = ""
    if populated_entries:
        pop_desc = ", ".join(f"{p.name} is populated ({p.data_rows} data rows)" for p in populated_entries)
        pop_summary = f"; {pop_desc} and holds the data."
    else:
        pop_summary = "."

    ans = prefix + "; ".join(status_phrases) + pop_summary
    cited_chunks = [row_chunk, inv_chunk] if row_chunk is not None else [inv_chunk]
    return {"answer": ans, "cited_chunks": cited_chunks}
