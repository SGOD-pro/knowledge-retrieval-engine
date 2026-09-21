"""Behavioral test suite reproducing the 9 evaluation defects identified in Phase 1.

These tests define the required correct evaluation behavior and are designed to fail
against the current evaluator before remediation.
"""

import json
from pathlib import Path
import pytest

from services.evaluation.benchmark_scorer import (
    grade_structured_numeric,
    grade_structured_json,
    grade_structured_premise,
    compute_retrieval_metrics,
)
from scripts.run_canonical_60_benchmark import resolve_ground_truth_chunk_ids
from schemas.models import Chunk


BACKEND_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BACKEND_DIR.parent
GROUND_TRUTH_FILE = BACKEND_DIR / "evaluation_assets" / "canonical_60_ground_truth.json"


@pytest.fixture(scope="module")
def ground_truth_data():
    with open(GROUND_TRUTH_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="module")
def test_suite_questions():
    with open(ROOT_DIR / "data" / "test.json", "r", encoding="utf-8") as f:
        data = json.load(f)
    return {q["id"]: q for q in data["questions"]}


# ---------------------------------------------------------------------------
# Defect 1: Q138 incorrectly accepts 28.50% instead of 28.09%
# ---------------------------------------------------------------------------
def test_defect_1_q138_rejects_loose_tolerance_28_50(ground_truth_data):
    """Q138 contract must reject 28.50% when the target is 28.09% with stated tolerance."""
    q138_contract = ground_truth_data["Q138"]["answer_contract"]
    
    # 28.09% should pass
    assert grade_structured_numeric("Operating margin was 28.09%", q138_contract) is True
    
    # Adversarial candidate 28.50% must FAIL (current evaluator accepts it due to max(tol, target*tol))
    assert grade_structured_numeric("Operating margin was 28.50%", q138_contract) is False


# ---------------------------------------------------------------------------
# Defect 2: Q145 accepts relative percentage growth instead of percentage-point difference
# ---------------------------------------------------------------------------
def test_defect_2_q145_rejects_relative_percentage_growth(ground_truth_data):
    """Q145 requires percentage points (30.67 pp) and must reject relative growth (50.07%)."""
    q145_contract = ground_truth_data["Q145"]["answer_contract"]
    
    # Relative percentage growth must be rejected
    adversarial_answer = "The relative growth in clean cooking fuel use is 50.07% between the districts (from 61.25% to 91.92%, difference of 30.67)."
    assert grade_structured_numeric(adversarial_answer, q145_contract) is False


# ---------------------------------------------------------------------------
# Defect 3: Q153 accepts an increase when the source shows a decrease
# ---------------------------------------------------------------------------
def test_defect_3_q153_rejects_increase_direction(ground_truth_data):
    """Q153 must enforce decrease direction; an answer claiming an increase must be rejected."""
    q153_contract = ground_truth_data["Q153"]["answer_contract"]
    
    # Stating increase instead of decrease must fail
    adversarial_answer = "Total income increased by 4,191 million dollars, representing a growth of 0.43%."
    assert grade_structured_numeric(adversarial_answer, q153_contract) is False


# ---------------------------------------------------------------------------
# Defect 4: Q170 accepts swapped vector count and dimensionality
# ---------------------------------------------------------------------------
def test_defect_4_q170_rejects_swapped_roles(ground_truth_data):
    """Q170 must bind 196 to vector count and 512 to dimension; swapped roles must fail."""
    q170_contract = ground_truth_data["Q170"]["answer_contract"]
    
    # Swapped vector count (512) and dimension (196) must FAIL
    adversarial_swapped = "There are 512 annotation vectors extracted per image, each 196-dimensional."
    assert grade_structured_numeric(adversarial_swapped, q170_contract) is False


# ---------------------------------------------------------------------------
# Defect 5: Q194 accepts wrong district, string int, extra keys, or Markdown fences
# ---------------------------------------------------------------------------
def test_defect_5_q194_exact_json_format_enforcement(ground_truth_data):
    """Q194 requires exact JSON object without fences, integer 108, correct district, no extra keys."""
    q194_contract = ground_truth_data["Q194"]["answer_contract"]
    
    # Case A: Wrong district
    wrong_district = '{"district": "South Andaman", "men_interviewed": 108}'
    assert grade_structured_json(wrong_district, q194_contract) is False, "Accepted wrong district"
    
    # Case B: String instead of integer
    string_int = '{"district": "North & Middle Andaman", "men_interviewed": "108"}'
    assert grade_structured_json(string_int, q194_contract) is False, "Accepted string instead of integer"
    
    # Case C: Extra keys
    extra_keys = '{"district": "North & Middle Andaman", "men_interviewed": 108, "status": "ok"}'
    assert grade_structured_json(extra_keys, q194_contract) is False, "Accepted extra keys"
    
    # Case D: Markdown fences (when strict format is specified)
    fenced_json = '```json\n{"district": "North & Middle Andaman", "men_interviewed": 108}\n```'
    assert grade_structured_json(fenced_json, q194_contract) is False, "Accepted markdown fences when forbidden"


# ---------------------------------------------------------------------------
# Defect 6: Q142 accepts an answer affirming the false million-dollar EPS premise
# ---------------------------------------------------------------------------
def test_defect_6_q142_rejects_affirming_false_premise(ground_truth_data):
    """Q142 must reject answers that affirm basic EPS is $2.03 million per share."""
    q142_contract = ground_truth_data["Q142"]["answer_contract"]
    
    adversarial_affirming = "Yes, basic earnings per share of $2.03 million per share is the correct reported unit in the filing."
    assert grade_structured_premise(adversarial_affirming, q142_contract) is False


# ---------------------------------------------------------------------------
# Defect 7: Reference answers for Q155, Q157, Q165, Q171, Q172, Q180 fail contracts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("qid", ["Q155", "Q157", "Q165", "Q171", "Q172", "Q180"])
def test_defect_7_reference_answers_must_pass_their_contracts(qid, ground_truth_data, test_suite_questions):
    """The canonical test suite expected_answer MUST satisfy the question's own premise contract."""
    expected_ans = test_suite_questions[qid]["expected_answer"]
    contract = ground_truth_data[qid]["answer_contract"]
    
    # Reference answer must pass grading
    assert grade_structured_premise(expected_ans, contract) is True, f"Reference answer for {qid} failed its own contract: {expected_ans}"


# ---------------------------------------------------------------------------
# Defect 8: Duplicate retrieved IDs inflate Recall and nDCG beyond 1
# ---------------------------------------------------------------------------
def test_defect_8_retrieval_metrics_duplicate_ids_never_exceed_one():
    """Retrieval metrics must deduplicate retrieved IDs so Recall and nDCG never exceed 1.0."""
    retrieved_with_dups = ["c1", "c1", "c1", "c1", "c1"]
    relevant = {"c1"}
    
    metrics = compute_retrieval_metrics(retrieved_with_dups, relevant)
    assert metrics["recall_at_5"] <= 1.0, f"Recall exceeded 1.0: {metrics['recall_at_5']}"
    assert metrics["ndcg_at_5"] <= 1.0, f"nDCG exceeded 1.0: {metrics['ndcg_at_5']}"
    assert metrics["precision_at_3"] <= 1.0, f"Precision exceeded 1.0: {metrics['precision_at_3']}"


# ---------------------------------------------------------------------------
# Defect 9: Wrong document hash or unresolvable required locator invalidates resolution
# ---------------------------------------------------------------------------
def test_defect_9_locator_resolution_validates_hash_and_all_required_locators():
    """Ground truth resolution must check document_sha256 and require ALL locators to resolve."""
    # Case A: Document hash mismatch must mark query unscorable or raise
    entries_hash_mismatch = {
        "Q_HASH": {
            "relevant_evidence": [
                {
                    "document_sha256": "wrong_hash_000000000000000000000000000000000000000000000000000000000000",
                    "source_filename": "f1.pdf",
                    "locator_type": "pdf_page",
                    "locator": "1",
                }
            ]
        }
    }
    chunks = [Chunk(id="c1", document_id="doc1", source_format="pdf", text="T", element_type="p", page_number=1)]
    doc_id_map = {"f1.pdf": "doc1"}
    file_hashes = {"f1.pdf": "real_hash_111111111111111111111111111111111111111111111111111111111111"}
    
    # Must fail or mark unscorable when hash mismatches
    resolved, unscorable = resolve_ground_truth_chunk_ids(
        entries_hash_mismatch, chunks, doc_id_map, doc_hashes=file_hashes
    )
    assert "Q_HASH" in unscorable or len(resolved.get("Q_HASH", set())) == 0

    # Case B: 1 of 2 required locators missing must invalidate resolution (not partially resolve)
    entries_partial = {
        "Q_MULTI": {
            "relevant_evidence": [
                {"document_sha256": "h1", "source_filename": "f1.pdf", "locator_type": "pdf_page", "locator": "1"},
                {"document_sha256": "h1", "source_filename": "f1.pdf", "locator_type": "pdf_page", "locator": "99"},  # Page 99 does not exist
            ]
        }
    }
    file_hashes_ok = {"f1.pdf": "h1"}
    resolved2, unscorable2 = resolve_ground_truth_chunk_ids(
        entries_partial, chunks, doc_id_map, doc_hashes=file_hashes_ok
    )
    assert "Q_MULTI" in unscorable2, "Query with unresolved required locator was not marked unscorable"
    assert len(resolved2.get("Q_MULTI", set())) == 0, "Partial resolution returned chunks when not all locators resolved"
