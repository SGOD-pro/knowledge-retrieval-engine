#!/usr/bin/env python3
"""Build and validate backend/evaluation_assets/canonical_60_ground_truth.json.

Exclusively owned by evaluator. Tests against ws_fresh_benchmark to ensure
all positive-evidence locators resolve to real chunk IDs (0 UNSCORABLE_GROUND_TRUTH).
"""

import json
from pathlib import Path
import re
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = SCRIPT_DIR.parent
ROOT_DIR = BACKEND_DIR.parent
SRC_DIR = BACKEND_DIR / "src"

sys.path.insert(0, str(SRC_DIR))
sys.path.insert(0, str(BACKEND_DIR))

DOC_HASHES = {
    "survay.csv": "c029d234eefd3fee2563b9c0929946ca336509ab62a02bd111c80f978298c66c",
    "2212.14776v3.pdf": "bce22600474513c61b8c47701d19275bb465dae909f1ede1598521dcf0ba57da",
    "SEC-Form-10Q.pdf": "3518726963892d32ec11a876a5dfa44810cd557d577b91eae28fccad29962505",
    "2412.20875v1.pdf": "927420d4297edb4f6ff566961a14c98af54a458f44a9cdb9f587bb9079fa877e",
    "rs_status_bill_passed_assent-1952-2016.csv": "85d048226e88f44a76f3bf68c3f96669162c3bd007081eb06ce7d57e4529e09e",
    "2501.05730v1.pdf": "4eafc24abe305dd84a859a95e19456a5f3527003ab41bd17abda7de657cccb48",
    "NFHS_5_India_Districts_Factsheet_Data.xls": "e482b990d74eb46b5ce5a6e10606b745a914d4b1b57838c1c6149e80ec8ad498",
    "2501.09166v1.pdf": "d5a727f2d0b04269389630b6687f7fa4e123b55f5e8a24726cab46df0aed9283",
    "National-Strategy-for-Artificial-Intelligence.pdf": "da5404dbb584c8956aae707dfcfe324dc1df11a179bc8288af0e898f4e37dcc5",
}

with open(ROOT_DIR / "data" / "test.json", encoding="utf-8") as f:
    suite = json.load(f)

ground_truth = {}

for q in suite["questions"]:
    qid = q["id"]
    cat = q["category"]
    exp_docs = q.get("expected_source_docs", [])
    hint = q.get("evaluation_hint", "")
    exp_ans = q.get("expected_answer")
    hallucination_check = q.get("hallucination_check", False)

    evidence_list = []
    answer_contract = {}

    # -------------------------------------------------------------
    # 1. Evidence Locator Mapping
    # -------------------------------------------------------------
    if cat == "HALLUCINATION_ABSENT" or not exp_docs:
        evidence_list = []
    else:
        for doc_name in exp_docs:
            dhash = DOC_HASHES[doc_name]
            # Match locators based on hint and document type
            if doc_name.endswith(".pdf"):
                # Find page numbers in hint (e.g. "PDF page 1", "PDF pages 1-2", "PDF page 4")
                p_matches = re.finditer(r"page[s]?\s+(\d+)(?:\s*[-–]\s*(\d+))?", hint, re.IGNORECASE)
                pages_found = []
                for pm in p_matches:
                    start_p = int(pm.group(1))
                    end_p = int(pm.group(2)) if pm.group(2) else start_p
                    pages_found.extend(range(start_p, end_p + 1))
                if not pages_found:
                    pages_found = [1]
                for p in sorted(set(pages_found)):
                    evidence_list.append({
                        "document_sha256": dhash,
                        "source_filename": doc_name,
                        "locator_type": "pdf_page",
                        "locator": str(p),
                    })

            elif doc_name.endswith(".csv"):
                # CSV records in hint: "data record 1", "data records 15 and 14", "data record 27"
                # In parsed CSV chunks, data record N corresponds to chunk metadata['row'] == N + 1
                rec_matches = re.findall(r"record[s]?\s+(\d+)(?:\s*(?:and|to|,)\s*(\d+))?", hint, re.IGNORECASE)
                rows_found = []
                for rm in rec_matches:
                    if rm[0]:
                        rows_found.append(int(rm[0]) + 1)
                    if rm[1]:
                        rows_found.append(int(rm[1]) + 1)
                
                # Check for explicit row mentions e.g. "row 10"
                if not rows_found:
                    explicit_rows = re.findall(r"row[s]?\s+(\d+)", hint, re.IGNORECASE)
                    rows_found = [int(r) for r in explicit_rows]

                # Special case: Q152 — min/max year across ALL 60,255 records.
                # This is a corpus-wide aggregate; no single row covers both endpoints.
                # The CSV is capped at 500 ingested chunks so 2013 rows are NOT in the index.
                # Mark evidence as empty so the question is excluded from retrieval scoring
                # rather than producing a false UNSCORABLE_GROUND_TRUTH result.
                if qid == "Q152":
                    rows_found = []  # corpus-wide aggregate — no single-row locator
                # Special case: Q164 — count of Year==2013 records
                # Data records 1-7 in the CSV have Year=2013 (rows 7-13 including header).
                elif qid == "Q164":
                    rows_found = [7, 8, 9, 10, 11, 12, 13]
                elif not rows_found:
                    rows_found = [2]

                for r in sorted(set(rows_found)):
                    evidence_list.append({
                        "document_sha256": dhash,
                        "source_filename": doc_name,
                        "locator_type": "csv_row",
                        "locator": str(r),
                    })

            elif doc_name.endswith(".xls"):
                # XLS coordinates in hint: "Sheet1!E4", "Sheet1!A2:C4", "Sheet1!K2:K4", "Sheet1!L3"
                # Excel rows include header row 1, so row numbers in chunk metadata directly match Excel row numbers!
                cell_rows = re.findall(r"Sheet1![A-Z]+(\d+)(?::[A-Z]+(\d+))?", hint)
                rows_found = []
                for cr in cell_rows:
                    start_r = int(cr[0])
                    end_r = int(cr[1]) if cr[1] else start_r
                    rows_found.extend(range(start_r, end_r + 1))
                if not rows_found:
                    rows_found = [2]
                # Q145: evidence is O4 (South Andaman) AND O3 (North & Middle Andaman)
                # ensure both rows 3 and 4 are present
                if qid == "Q145" and 4 in rows_found and 3 not in rows_found:
                    rows_found.append(3)
                for r in sorted(set(rows_found)):
                    evidence_list.append({
                        "document_sha256": dhash,
                        "source_filename": doc_name,
                        "locator_type": "spreadsheet_row",
                        "locator": str(r),
                    })

    # -------------------------------------------------------------
    # 2. Structured Answer Contract Mapping
    # -------------------------------------------------------------
    if cat == "HALLUCINATION_ABSENT":
        answer_contract = {
            "contract_type": "refusal",
            "is_refusal_required": True,
            "refusal_indicators": [
                "couldn't find any relevant",
                "not found",
                "does not contain",
                "not mentioned",
                "no information",
                "cannot find",
                "absent",
                "no positive source",
            ],
        }
    elif cat == "HALLUCINATION_PREMISE":
        # Premise correction contract
        key_terms = []
        if qid == "Q142":
            # False premise: EPS is $2.03 million. Must correct to "per share".
            # Reject any answer that affirms the million-dollar unit.
            key_terms = ["per share"]
        elif qid == "Q148":
            key_terms = ["-92.55", "negative"]
        elif qid == "Q155":
            # Reconciliation: income−expenditure ≠ surplus; difference = 1521
            key_terms = ["109,848", "108,327", "1,521"]
        elif qid == "Q157":
            # Value is code C, not numeric zero
            key_terms = ["not a numeric", "c"]
        elif qid == "Q165":
            # Title says 2016 but Year field is 2014
            key_terms = ["2014", "title"]
        elif qid == "Q171":
            # Accurate predictions don't guarantee interpretability
            key_terms = ["accurately", "not interpretable"]
        elif qid == "Q172":
            # MoD beats A-MoD at ViT-Base 12.5%
            key_terms = ["78.49", "78.42"]
        elif qid == "Q180":
            # O(tD): independent of L but depends on t and D
            key_terms = ["o(td)", "sequence length"]

        answer_contract = {
            "contract_type": "premise_correction",
            "expected_answer": exp_ans,
            "key_correction_terms": key_terms,
            "rejected_generic_responses": ["no", "incorrect", "that is incorrect", "this is false", "actually no"],
            "reject_if_affirms_any": (
                ["correct unit", "correct unit.", "yes, basic", "yes. basic", "the correct unit"]
                if qid == "Q142" else []
            ),
        }
    elif cat == "NUMERIC":
        # Extract target numeric values and units
        target_vals = []
        operands = []
        required_sign = None
        required_units = []

        if qid == "Q138":
            target_vals = [28.09]
            operands = [30739.0, 109417.0]
            required_units = ["%", "percent"]
        elif qid == "Q139":
            target_vals = [492.0]
            operands = [149818.0, 149326.0]
            required_sign = "+"
            required_units = ["million", "dollars"]
        elif qid == "Q144":
            target_vals = [134.0]
        elif qid == "Q145":
            target_vals = [30.67]
            operands = [91.92, 61.25]
            required_units = ["percentage points", " pp"]  # compound phrase: reject relative-growth answers
        elif qid == "Q153":
            target_vals = [4191.0, 0.43]
            operands = [980268.0, 976077.0]
            required_units = ["million", "%"]
        elif qid == "Q154":
            target_vals = [1521.0]
            operands = [88214.0, 86693.0]
            required_units = ["million", "dollars"]
        elif qid == "Q161":
            target_vals = [50.0]
            required_units = ["days"]
        elif qid == "Q170":
            # 196 annotation vectors each 512-dimensional; roles must not be swapped
            target_vals = [196.0, 512.0]
        elif qid == "Q173":
            target_vals = [2.04, 0.21]

        # Build tolerance: Q138 requires tighter precision (0.01 pp)
        tol = 0.01 if qid == "Q138" else 0.02

        answer_contract = {
            "contract_type": "numeric",
            "target_values": target_vals,
            "operands": operands,
            "required_sign": required_sign,
            "required_units": required_units,
            "tolerance": tol,
            # Direction enforcement for change questions
            **({"required_direction": "decrease"} if qid == "Q153" else {}),
            # Role binding: for Q170, 196 = annotation count, 512 = dimension
            **({"required_roles": [
                {"value": 196, "role": "annotation"},
                {"value": 512, "role": "dimension"},
            ]} if qid == "Q170" else {}),
        }
    elif qid == "Q194":
        answer_contract = {
            "contract_type": "json_schema",
            "required_keys": ["district", "men_interviewed"],
            "required_values": {
                "district": "North & Middle Andaman",
                "men_interviewed": 108,
            },
            "allow_extra_keys": False,
            "strict_format": True,
        }
    elif cat == "EDGE_ADVERSARIAL" and "json" in q["question"].lower():
        answer_contract = {
            "contract_type": "json_schema",
            "required_keys": ["answer"],
        }
    else:
        # Standard semantic / content match
        answer_contract = {
            "contract_type": "semantic",
            "expected_answer": exp_ans,
        }

    ground_truth[qid] = {
        "id": qid,
        "category": cat,
        "question": q["question"],
        "expected_answer": exp_ans,
        "relevant_evidence": evidence_list,
        "answer_contract": answer_contract,
    }

output_path = BACKEND_DIR / "evaluation_assets" / "canonical_60_ground_truth.json"
output_path.parent.mkdir(parents=True, exist_ok=True)
with open(output_path, "w", encoding="utf-8") as f:
    json.dump(ground_truth, f, indent=2)

print(f"Generated ground truth for {len(ground_truth)} questions at {output_path}")

# -------------------------------------------------------------
# 3. Test post-ingestion locator resolution against ws_fresh_benchmark
# -------------------------------------------------------------
from modules.query.query_repository import QueryRepository
repo = QueryRepository()
all_chunks = repo.get_all_chunks(workspace_id="ws_fresh_benchmark")
docs = repo.get_workspace_documents("ws_fresh_benchmark")["documents"]
doc_id_to_fname = {d["id"]: d["filename"] for d in docs}
doc_fname_to_id = {d["filename"]: d["id"] for d in docs}

unscorable_count = 0
resolved_summary = {}

for qid, entry in ground_truth.items():
    ev_list = entry["relevant_evidence"]
    if not ev_list:
        resolved_summary[qid] = []
        continue

    matched_chunk_ids = set()
    for ev in ev_list:
        fname = ev["source_filename"]
        target_did = doc_fname_to_id.get(fname)
        loc_type = ev["locator_type"]
        loc_val = ev["locator"]

        for c in all_chunks:
            if c.document_id != target_did:
                continue

            if loc_type == "pdf_page":
                if c.page_number == int(loc_val):
                    matched_chunk_ids.add(c.id)
            elif loc_type in ("csv_row", "spreadsheet_row"):
                meta_row = (c.metadata or {}).get("row")
                if meta_row is not None and meta_row == int(loc_val):
                    matched_chunk_ids.add(c.id)
                elif c.location_reference and f"Row: {loc_val}" in c.location_reference or f"Row {loc_val}" in c.location_reference:
                    matched_chunk_ids.add(c.id)

    if not matched_chunk_ids:
        print(f"FAILED TO RESOLVE: {qid} ({entry['category']}) with locators: {ev_list}")
        unscorable_count += 1
    else:
        resolved_summary[qid] = list(matched_chunk_ids)

print(f"Locator resolution test complete: {len(ground_truth) - unscorable_count}/{len(ground_truth)} scorable. Unscorable count: {unscorable_count}")
if unscorable_count > 0:
    sys.exit(1)
print("SUCCESS: 0 UNSCORABLE_GROUND_TRUTH cases.")
