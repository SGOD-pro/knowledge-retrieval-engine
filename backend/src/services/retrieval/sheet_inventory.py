"""Version-bound workbook sheet inventory resolution and evidence construction."""

from __future__ import annotations

import re
from typing import Any

from schemas.models import Chunk
from services.retrieval.evidence_contract import EvidenceItem, evidence_item_from_chunk

_EMPTY_OR_ABSENT_SHEET_RE = re.compile(
    r"\b(sheet\s*[a-z0-9_]+(?:\s*(?:,|and)\s*sheet\s*[a-z0-9_]+)*)\s+(?:is\s+|are\s+)?(?:empty|blank|have\s+0\s+data\s+rows|has\s+0\s+data\s+rows|have\s+no\s+data|has\s+no\s+data|contain\s+no\s+data|contains\s+no\s+data|absent|do\s+not\s+exist|does\s+not\s+exist|missing)\b",
    re.IGNORECASE,
)


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
    answer: str,
    retained_evidence_items: list[Any],
    citations: list[dict[str, Any]] | None = None,
) -> tuple[bool, str | None]:
    """Verify sheet-status claims against retained sheet_inventory and populated-sheet row evidence."""
    if not answer or answer == "NOT_FOUND":
        return True, None

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

    matches = list(_EMPTY_OR_ABSENT_SHEET_RE.finditer(answer))
    if not matches and not inv_items:
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

    for m in matches:
        clause = m.group(0).lower()
        expects_absent = any(k in clause for k in ("absent", "does not exist", "do not exist", "missing"))
        sheet_names = re.findall(r"sheet\s*[a-z0-9_]+", m.group(1), re.IGNORECASE)
        for raw_name in sheet_names:
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

    # If the answer also references a populated sheet from the inventory, require a cited row/cell chunk
    for ev in inv_items:
        ev_did = str(getattr(ev, "document_id", "") or "")
        inv = ((getattr(ev, "provenance", {}) or {}).get("metadata") or {}).get("sheet_inventory") or {}
        populated_names = [
            str(s.get("name", ""))
            for s in (inv.get("sheets") or [])
            if s.get("status") == "populated" and s.get("name")
        ]
        if any(re.search(rf"\b{re.escape(name)}\b", answer, re.IGNORECASE) for name in populated_names):
            has_row_cit = any(
                str(getattr(c_ev, "document_id", "") or "") == ev_did
                and c_ev not in inv_items
                for c_ev in cited_ev_items
            )
            if not has_row_cit:
                return False, "missing_populated_sheet_row_support"

    return True, None
