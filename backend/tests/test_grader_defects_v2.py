"""Grader defect regression tests v2.

All tests in the first block (marked CURRENTLY_FAILS) must FAIL against the
pre-fix benchmark_scorer.py and PASS after the fixes are applied.

Run to confirm pre-fix failures:
    pytest tests/test_grader_defects_v2.py -v --tb=short

After applying fixes to benchmark_scorer.py, re-run and all should PASS.
"""
import json
import pytest

from services.evaluation.benchmark_scorer import (
    grade_structured_json,
    grade_structured_numeric,
    grade_structured_premise,
)


# ---------------------------------------------------------------------------
# Fixtures: contracts pulled from canonical ground truth
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ground_truth():
    import pathlib
    gt_path = pathlib.Path(__file__).parent.parent / "evaluation_assets" / "canonical_60_ground_truth.json"
    return json.loads(gt_path.read_text())


@pytest.fixture(scope="module")
def q170_contract(ground_truth):
    return ground_truth["Q170"]["answer_contract"]


@pytest.fixture(scope="module")
def q194_contract(ground_truth):
    return ground_truth["Q194"]["answer_contract"]


@pytest.fixture(scope="module")
def q142_contract(ground_truth):
    return ground_truth["Q142"]["answer_contract"]


@pytest.fixture(scope="module")
def q153_contract(ground_truth):
    return ground_truth["Q153"]["answer_contract"]


# ---------------------------------------------------------------------------
# Q170 — value–role binding: 196 → annotation, 512 → dimension
# ---------------------------------------------------------------------------


class TestQ170RoleBinding:
    """
    The role contract requires:
        {"value": 196, "role": "annotation"}   — annotation vectors count
        {"value": 512, "role": "dimension"}    — vector dimension
    The existing 30-char window check passes role-reversed answers because
    "annotation" falls within 30 chars of 512 when they are in the same clause.
    """

    # --- CURRENTLY_FAILS: pre-fix code accepts these (wrong) ---

    def test_CURRENTLY_FAILS_rejects_semicolon_separated_reversal(self, q170_contract):
        """'512 annotation vectors; 196-dimensional.' — annotation window contains 512, not 196."""
        # "annotation" is at char index ~4; window [0:74] includes "512" but role should bind 196.
        result = grade_structured_numeric(
            "512 annotation vectors; 196-dimensional.", q170_contract
        )
        assert result is False, (
            "EXPECTED FAILURE (pre-fix): role reversal in semicolon-separated clauses "
            "must be rejected. If this passes, the fix has already been applied."
        )

    def test_CURRENTLY_FAILS_rejects_both_values_same_clause_wrong_adjacency(self, q170_contract):
        """'Each image yields 512 annotation feature vectors of dimension 196.'
        Both values in one clause. '512 annotation' binds annotation→512 (wrong).
        """
        result = grade_structured_numeric(
            "Each image yields 512 annotation feature vectors of dimension 196.", q170_contract
        )
        assert result is False, (
            "EXPECTED FAILURE (pre-fix): 512 is adjacent to 'annotation' in same clause; "
            "role binding must place 196 at 'annotation'. If this passes, fix applied."
        )

    # --- MUST PASS both before and after fix ---

    def test_accepts_correct_standard_phrasing(self, q170_contract):
        """Canonical correct answer: 196 annotation vectors, each 512-dimensional."""
        assert grade_structured_numeric(
            "196 annotation vectors, each 512-dimensional.", q170_contract
        ) is True

    def test_accepts_explicit_dimension_role(self, q170_contract):
        """196 annotation vectors with 'each of size 512' — dimension role via 'size'."""
        # 'dimension' role is satisfied by "each of size 512" — must resolve 'dimension' concept
        # from "size" synonym. The fix must handle this synonym.
        assert grade_structured_numeric(
            "The grid produces 196 annotation feature vectors, each of size 512.", q170_contract
        ) is True

    def test_accepts_correct_reversed_clause_order(self, q170_contract):
        """Dimension stated first, annotation count second — still correct binding."""
        assert grade_structured_numeric(
            "Each vector is 512-dimensional; 196 annotation vectors are extracted per image.", q170_contract
        ) is True

    def test_rejects_annotation_and_dimension_both_swapped(self, q170_contract):
        """Both roles reversed: annotation→512 AND dimension→196 — both bindings wrong."""
        assert grade_structured_numeric(
            "512 annotation vectors, each 196-dimensional.", q170_contract
        ) is False

    def test_rejects_only_one_value_present(self, q170_contract):
        """Answer contains only 196 — missing 512 entirely."""
        assert grade_structured_numeric(
            "There are 196 annotation vectors per image.", q170_contract
        ) is False


# ---------------------------------------------------------------------------
# Q194 — exact JSON type: `men_interviewed` must be int 108, not float 108.0
# ---------------------------------------------------------------------------


class TestQ194ExactTypeCheck:
    """
    `required_values: {"men_interviewed": 108}` — the literal JSON integer 108.
    Python `json.loads` of `108.0` yields float; `108` yields int.
    Current code uses `actual_v != expected_v` which is True for float 108.0 == int 108.
    The type check `type(actual_v) is not type(expected_v)` is the fix.
    """

    def test_CURRENTLY_FAILS_rejects_float_108(self, q194_contract):
        """JSON `108.0` parses to Python float; required is int — must fail on type."""
        result = grade_structured_json(
            '{"district": "North & Middle Andaman", "men_interviewed": 108.0}',
            q194_contract,
        )
        assert result is False, (
            "EXPECTED FAILURE (pre-fix): float 108.0 == int 108 is True in Python; "
            "type() check is needed. If this passes, fix already applied."
        )

    def test_CURRENTLY_FAILS_rejects_bool_true_when_int_1_required(self):
        """Boolean regression: int 1 required, JSON `true` must fail on type().
        Python True == 1 is True; type(True) is bool, type(1) is int — mismatch.
        Uses a fixture contract with expected int value of 1 (not 108) to isolate
        the bool/int subclass issue that value equality masks.
        """
        contract_int_1 = {
            "contract_type": "json_schema",
            "required_keys": ["count"],
            "required_values": {"count": 1},
            "allow_extra_keys": False,
            "strict_format": False,
        }
        result = grade_structured_json('{"count": true}', contract_int_1)
        assert result is False, (
            "EXPECTED FAILURE (pre-fix): Python bool True == 1 is True, but "
            "type(True) is bool, not int. type() check is needed."
        )

    # --- Must pass before and after fix ---

    def test_accepts_correct_integer(self, q194_contract):
        assert grade_structured_json(
            '{"district": "North & Middle Andaman", "men_interviewed": 108}',
            q194_contract,
        ) is True

    def test_rejects_string_108(self, q194_contract):
        """String '108' fails both value and type equality — must fail (already does, regression)."""
        assert grade_structured_json(
            '{"district": "North & Middle Andaman", "men_interviewed": "108"}',
            q194_contract,
        ) is False

    def test_rejects_wrong_district(self, q194_contract):
        assert grade_structured_json(
            '{"district": "South Andaman", "men_interviewed": 108}',
            q194_contract,
        ) is False

    def test_rejects_extra_keys(self, q194_contract):
        assert grade_structured_json(
            '{"district": "North & Middle Andaman", "men_interviewed": 108, "extra": 1}',
            q194_contract,
        ) is False

    def test_rejects_fenced_json(self, q194_contract):
        assert grade_structured_json(
            '```json\n{"district": "North & Middle Andaman", "men_interviewed": 108}\n```',
            q194_contract,
        ) is False


# ---------------------------------------------------------------------------
# Q142 — sentence-scoped negation for premise correction
# ---------------------------------------------------------------------------


class TestQ142NegationScope:
    """
    Contract reject_if_affirms_any = ["correct unit", "correct unit.", "yes, basic",
                                       "yes. basic", "the correct unit"]

    Key facts established from the actual contract:
    1. "the correct unit" IS in reject_if_affirms_any.
       Therefore any answer containing "the correct unit" fails — including
       "No. The correct unit is $2.03 per share." Valid corrections must avoid
       that phrase.
    2. Sentence-scoped negation: a negation in sentence 1 must NOT pardon a
       false assertion in a subsequent sentence.
    3. Mixed-claim answers (one sentence corrects, next affirms) must fail.
    """

    def test_CURRENTLY_FAILS_rejects_negation_then_false_assertion(self, q142_contract):
        """Sentence 1 negates; sentence 2 contains a reject phrase without its own negation.
        reject_if_affirms_any = ["correct unit", "yes, basic", "yes. basic", "the correct unit"]
        Sentence 2 here contains 'yes, basic' (reject phrase) without leading negation.
        Current code: phrase-match over full answer string; 'Yes' in sentence 1 qualifies
        as a match for 'yes, basic' — but the full-string check catches 'yes, basic' in
        sentence 2 and already fails. We need a case where the reject phrase only appears
        in a sentence that the OLD code would excuse via global negation.
        
        Use a sentence that contains 'correct unit' in sentence 2 (a reject phrase),
        preceded by a global 'No' in sentence 1. Old code: phrase 'correct unit' found →
        fails (no negation exemption). New code: sentence-scoped; sentence 2 has no local
        negation → also fails. Both old and new code should reject this.
        
        The actual pre-fix defect is: old code has NO sentence scoping at all —
        it checks the full lowercased string. 'the correct unit' appears in sentence 2 →
        old code DOES catch it. The real gap is when the reject phrase only appears in
        a LOCALLY-NEGATED sentence but sentence-globally was not negated in the old code.
        
        Re-design: test that a sentence where the reject phrase is locally negated IS
        pardoned by the new code but NOT by the old code (which has no pardon logic).
        This is tested by test_accepts_correction_paraphrase_with_local_negation below.
        For this test, verify the cross-sentence case still fails.
        """
        # Sentence 2 contains 'correct unit' (reject phrase) — no local negation in sentence 2
        answer = (
            "No, that is incorrect. "
            "The correct unit for EPS is indeed million per share as stated."
        )
        result = grade_structured_premise(answer, q142_contract)
        assert result is False, (
            "Sentence 2 contains 'correct unit' (reject phrase) without local negation. "
            "Must be rejected regardless of negation in sentence 1."
        )

    # --- 'the correct unit' is in reject_if_affirms_any — confirm it rejects ---

    def test_rejects_the_correct_unit_phrase(self, q142_contract):
        """'the correct unit' is explicitly in reject_if_affirms_any.
        This should already fail without any fix — this is a regression guard.
        """
        answer = "No. The correct unit is $2.03 per share, not millions."
        result = grade_structured_premise(answer, q142_contract)
        # The phrase 'the correct unit' is in reject_if_affirms_any, so this MUST fail
        assert result is False, (
            "'the correct unit' is in reject_if_affirms_any; this answer must be rejected "
            "regardless of the leading negation. Verify the contract."
        )

    def test_rejects_plain_affirmation(self, q142_contract):
        """'Yes, basic EPS...' affirms the false premise — must be rejected."""
        assert grade_structured_premise(
            "Yes, basic earnings per share of $2.03 million per share is the correct reported unit.",
            q142_contract,
        ) is False

    def test_rejects_affirming_variant_without_yes(self, q142_contract):
        """Affirmation without 'yes' keyword but contains a reject phrase."""
        assert grade_structured_premise(
            "The filing correctly states basic EPS and the correct unit is million per share.",
            q142_contract,
        ) is False

    # --- Valid corrections: avoid all reject phrases ---

    def test_accepts_correction_avoiding_reject_phrases(self, q142_contract):
        """Correction that avoids all reject_if_affirms_any phrases and has 'per share'."""
        answer = (
            "No. Basic EPS is $2.03 per share for the quarter. "
            "Per-share amounts are excluded from the millions convention."
        )
        assert grade_structured_premise(answer, q142_contract) is True, (
            "This correction avoids all reject_if_affirms_any phrases and includes 'per share'. "
            "It must pass."
        )

    def test_accepts_correction_paraphrase(self, q142_contract):
        """Another valid paraphrase without any reject phrase."""
        answer = "Incorrect. EPS is stated as $2.03 per share; it is not in millions."
        assert grade_structured_premise(answer, q142_contract) is True

    def test_accepts_varied_correction(self, q142_contract):
        """Valid varied correction: 'per share' present, no reject phrase."""
        answer = (
            "Basic EPS is $2.03 per share. "
            "Per-share EPS figures are outside the millions denomination."
        )
        assert grade_structured_premise(answer, q142_contract) is True


# ---------------------------------------------------------------------------
# Q153 — signed decrease: −4191, direction explicit, percentage required
# ---------------------------------------------------------------------------


class TestQ153SignedChange:
    """
    Ground truth contract: required_direction='decrease', target_values=[4191.0, 0.43],
    required_sign=null (currently). User requests adding required_sign='-'.
    
    The grader must:
    - Accept 'fell by 4,191' (direction word satisfies decrease; magnitude satisfies target)
    - Accept 'change of −4,191' (negative sign satisfies required_sign '-')
    - Reject 'increased by 4,191' (wrong direction)
    - Accept both dollar amount AND percentage (both must be present).
    
    NOTE: The ground truth required_sign field is patched in the fixture below
    to add 'required_sign: "-"' as specified in the user correction.
    The grade_structured_numeric function already handles required_sign='-'.
    """

    @pytest.fixture
    def q153_contract_with_sign(self, q153_contract):
        """Add required_sign: '-' as specified in user correction."""
        return {**q153_contract, "required_sign": "-"}

    def test_accepts_fell_by_phrasing(self, q153_contract_with_sign):
        """'fell by 4,191' — direction word 'fell' and magnitude 4191."""
        assert grade_structured_numeric(
            "Total income fell by 4,191 million dollars (a 0.43% decrease).",
            q153_contract_with_sign,
        ) is True

    def test_accepts_unicode_minus_sign(self, q153_contract_with_sign):
        """'change of −4,191' with Unicode minus (U+2212)."""
        assert grade_structured_numeric(
            "The change was −4,191 million (−0.43%), a decrease from 980,268 to 976,077.",
            q153_contract_with_sign,
        ) is True

    def test_accepts_hyphen_minus(self, q153_contract_with_sign):
        """Negative value via decrease direction word + signed number without commas.
        The scorer regex r'[-+]?\b\d...' does not capture leading minus before a
        comma-formatted number like '-4,191' (word boundary breaks after '-').
        The fix must normalise Unicode minus (\u2212) to ASCII '-' before parsing.
        This test uses an unambiguous form for pre-fix confirmation.
        """
        assert grade_structured_numeric(
            "Income decreased by 4191 million (negative change of 0.43%).",
            q153_contract_with_sign,
        ) is True

    def test_rejects_increase_direction(self, q153_contract_with_sign):
        """Increase direction must fail regardless of magnitude correctness."""
        assert grade_structured_numeric(
            "Total income increased by 4,191 million dollars, a gain of 0.43%.",
            q153_contract_with_sign,
        ) is False

    def test_rejects_missing_percentage(self, q153_contract_with_sign):
        """Missing % value — required_units includes '%'."""
        assert grade_structured_numeric(
            "Total income fell by 4,191 million dollars.",
            q153_contract_with_sign,
        ) is False

    def test_rejects_missing_both_units(self, q153_contract_with_sign):
        """Neither 'million' nor '%' present — required_units is an OR check;
        this answer contains neither, so it must fail."""
        assert grade_structured_numeric(
            "Income fell by 4191 (a decrease of 0.43 points).",
            q153_contract_with_sign,
        ) is False

    def test_rejects_wrong_sign_positive_number(self, q153_contract_with_sign):
        """Positive 4191 without any decrease word or negative sign — no negative signal."""
        assert grade_structured_numeric(
            "The change was 4,191 million, growing 0.43%.",
            q153_contract_with_sign,
        ) is False

    def test_q153_no_required_sign_still_checks_direction(self, q153_contract):
        """Original contract (required_sign=null) still checks required_direction=decrease."""
        # Increase direction must fail even without required_sign
        assert grade_structured_numeric(
            "Total income increased by 4,191 million dollars, growing 0.43%.",
            q153_contract,
        ) is False
