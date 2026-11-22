# Audit: Actual Correctness of Canonical Run 20261122_182354

**Author**: Senior Software Engineer
**Date**: 2026-11-22
**Branch**: `re-factor`
**Inspected HEAD**: `42c35334c44bcda1bc5a58d6fc31b1c7426b3c4f`
**Supersedes**: Row reference descriptions in [docs/reviews/2026-11-22-re-factor-exact-row-resolver-and-canonical-rescore.md](file:///home/swyra/projects/knowledge-retrieval-engine/docs/reviews/2026-11-22-re-factor-exact-row-resolver-and-canonical-rescore.md)

## Executive Summary

This audit provides a question by question inspection of all 60 answers in cold canonical benchmark run `20261122_182354`. Each answer was audited against its answer contract, its retrieved citations, and the authoritative source documents.
Original run scores and files are preserved without modification. All checks were conducted offline with zero benchmark reruns, zero provider calls, and zero ground truth edits.

### Key Audit Metrics

* Total questions audited: 60
* Original evaluator score: 32 out of 60 correct (53.33% accuracy)
* Audited grounded answers: 34 out of 60 factually supported (56.67% accuracy)
* False passes in evaluator: 1 (Q142: passed via table text dump without refuting false premise)
* False failures in evaluator: 3 (Q143, Q149, Q157: factually correct answers penalized by rigid string or token overlap thresholds)
* True passes: 31 questions
* True failures: 23 questions
* Unverified due to missing evidence retrieval: 2 questions (Q190, Q192: cross document retrieval gaps led to model refusal)
* Structured aggregate unanswerable query: Q159 correctly returned 0 matching rows for 2026

## Complete 60 Question Audit Table

| Question ID | Actual Answer | Original Grade | Audited Verdict | Supporting Source Locator | Reason |
|---|---|---|---|---|---|
| Q136 | 001-36743 | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 1) | Direct factual match on page 1 Commission File Number. |
| Q137 | 14,594,180,000 shares | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 2) | Direct factual match on page 2 common stock outstanding as of July 17, 2026. |
| Q138 | 28.09%, calculated as 30739 / 109417 × 100 | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 4, pdf_page 18) | Correct arithmetic ratio of Services net sales (30,739) to total net sales (109,417). |
| Q139 | 492 million dollars: 149818 minus 149326 | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 6) | Correct balance sheet arithmetic: Current assets 149,818 minus current liabilities 149,326 = 492 million dollars. |
| Q140 | Inventory increased by $5,374 million from September 27, 2025 to June 27, 2026. | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 6) | Correct delta: Inventories rose from 5,718 to 11,092 (+5,374 million dollars). |
| Q141 | For the three months ended June 28, 2025, other income/(expense), net was neg... | PASS | PASS | SEC-Form-10Q.pdf (pdf_page 4) | Correct sign and amount from income statement page 4. |
| Q142 | Note 3 – Earnings Per Share The following table shows the computation of basi... | PASS | FAIL | SEC-Form-10Q.pdf (pdf_page 4) | False Pass. Model never refuted the false premise or answered whether $2.03 million is the correct unit. It dumped Note 3 table text, which passed only because the table row header contained 'per share'. |
| Q143 | Apple's gross margin in the June 2026 quarter was $54,770 million in dollar a... | FAIL | PASS | SEC-Form-10Q.pdf (pdf_page 19) | False Failure. Model provided exact dollar gross margin (54,770 million) and percentage rounded to 1 decimal (50.1% vs 50.06%). Failed originally only due to rigid token/formula overlap in content_match. |
| Q144 | 134 | PASS | PASS | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 4) | Exact integer value from South Andaman row 4 column 5. |
| Q145 | The gap in clean cooking fuel use between South Andaman and North & Middle An... | PASS | PASS | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 3, spreadsheet_row 4) | Correct difference calculation between South Andaman (91.92%) and North & Middle Andaman (61.25%). |
| Q146 | Total 2,624: North & Middle Andaman 874 + Nicobars 882 + South Andaman 868 | PASS | PASS | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 2, spreadsheet_row 3, spreadsheet_row 4) | Correct sum of households across the three Andaman & Nicobar rows: 882 + 874 + 868 = 2,624. |
| Q147 | North District has the highest percentage of children under five with registe... | FAIL | FAIL | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 3) | True failure. Model cited row 526 in Sikkim (North District, 98.14%) instead of North & Middle Andaman (row 3, 100.00%). |
| Q148 | District Names: North & Middle Andaman... Deaths in the last 3 years register... | FAIL | FAIL | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 3) | True failure. Model dumped row text containing  92.55 but failed to assess whether the negative value is safe to interpret literally. |
| Q149 | 99.58% | FAIL | PASS | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 4) | False Failure. Model gave the exact percentage 99.58% grounded in South Andaman row 4. Evaluator failed it because concise numeric output missed English prompt prose tokens in content_match. |
| Q150 | Codes for Reproducing Results All the datasets and codes are available here. ... | FAIL | FAIL | NFHS_5_India_Districts_Factsheet_Data.xls | True failure. Irrelevant hallucinated PyTorch and GPU text. |
| Q151 | I couldn't find any relevant passages in the workspace documents matching you... | PASS | PASS | None (unanswerable / refusal) | Grounded refusal. Correlation coefficients across respondents are absent from district aggregate tables. |
| Q152 | 2013 and 2025. | PASS | PASS | survay.csv (structured aggregate) | Correct earliest and latest years present in survay.csv. |
| Q153 | It fell by 4,191 Dollars (millions), from 980,268 to 976,077, a decrease of 0... | PASS | PASS | survay.csv (structured aggregate) | Correct delta and percentage drop for All industries income between 2024 and 2025. |
| Q154 | 1521 million dollars: 88214 minus 86693 | PASS | PASS | survay.csv (csv_row 15, csv_row 16) | Correct arithmetic: closing stocks 88,214 minus opening stocks 86,693 = 1,521 million dollars. |
| Q155 | The sum for Value is 16790554. | FAIL | FAIL | survay.csv | True failure. Returned unprompted column sum instead of reconciling surplus before income tax. |
| Q156 | I couldn't find any relevant passages in the workspace documents matching you... | FAIL | FAIL | survay.csv (csv_row 44) | True failure due to retrieval miss. Model refused because retrieval failed to provide row 44 (Value 79, Unit Percentage). |
| Q157 | No, it is not correct. The value for H05 in 2025 Beverage and Tobacco Product... | FAIL | PASS | survay.csv (csv_row 499) | False Failure. Model correctly refutes premise and identifies that 'C' represents confidential data. Evaluator failed it because it omitted the specific phrase 'not a numeric'. |
| Q158 | I couldn't find any relevant passages in the workspace documents matching you... | FAIL | FAIL | rs_status_bill_passed_assent-1952-2016.csv | True failure. Model refused instead of requesting title/year disambiguation. |
| Q159 | No rows matched the requested filters in table 'survay' (0 matched)... | FAIL | PASS | None (unanswerable / refusal) | False Failure. Data for 2026 is absent from survay.csv. Structured table execution correctly reported 0 matching rows, which constitutes a valid grounded refusal. |
| Q160 | 17/12/2014: introduction; 4/5/2016: Rajya Sabha passage; 9/5/2016: Lok Sabha ... | PASS | PASS | rs_status_bill_passed_assent-1952-2016.csv (csv_row 4) | Correct chronological legislative milestones from Anti Hijacking Bill row 4. |
| Q161 | 50 days, from 16 March 2016 to 5 May 2016 | PASS | PASS | rs_status_bill_passed_assent-1952-2016.csv (csv_row 2) | Correct elapsed day calculation for Sikh Gurdwaras Bill row 2. |
| Q162 | Bill No.: XXXVI. Short Title of the Bill: The Anti-Hijacking Bill, 2016... | FAIL | FAIL | rs_status_bill_passed_assent-1952-2016.csv (csv_row 4) | True failure. Model pasted row details without addressing whether a blank cell proves lack of committee referral. |
| Q163 | 13/05/2016 | FAIL | FAIL | rs_status_bill_passed_assent-1952-2016.csv (csv_row 2) | True failure. Model picked row 227 assent date instead of disambiguating multiple Bill No. LIII entries. |
| Q164 | 0 | FAIL | FAIL | rs_status_bill_passed_assent-1952-2016.csv | True failure. Model reported 0 bills passed in 2016 when the CSV contains 7. |
| Q165 | No, the Anti-Hijacking Bill with the year 2016 in its title was introduced on... | PASS | PASS | rs_status_bill_passed_assent-1952-2016.csv (csv_row 4) | Correct premise refutation stating introduction occurred in 2014 despite 2016 in title. |
| Q166 | I couldn't find any relevant passages in the workspace documents matching you... | PASS | PASS | None (unanswerable / refusal) | Grounded refusal. Member vote tallies are not captured in the legislative CSV. |
| Q167 | Selective Dependence Classification | PASS | PASS | 2212.14776v3.pdf (pdf_page 2) | Exact match to Section 3 concept in interpretability paper. |
| Q168 | Fully Convolutional Attention Mechanism | FAIL | FAIL | 2212.14776v3.pdf (pdf_page 2) | True failure. Hallucinated acronym expansion (correct is Focus Classify Attention Model). |
| Q169 | The average attention values for full, random 20, and top 20 are 0.539, 0.24,... | PASS | PASS | 2212.14776v3.pdf (pdf_page 6, Table 1) | Exact values from Table 1 attention analysis. |
| Q170 | In the image-captioning case study, 196 annotation vectors are extracted per ... | PASS | PASS | 2212.14776v3.pdf (pdf_page 10) | Exact vector count (196) and dimensionality (512) from Section 4.2. |
| Q171 | On the Interpretability of Attention Networks 0 4 8 X1 6 0 4 X2 fg_1 fg_2 fg_... | FAIL | FAIL | 2212.14776v3.pdf (pdf_page 1) | True failure. Raw OCR snippet dump that failed to address the question. |
| Q172 | No, A-MoD beats standard MoD in every Table 1 configuration. | FAIL | FAIL | 2412.20875v1.pdf (pdf_page 6, Table 1) | True failure. Contradictory wording and factually inaccurate (MoD outperforms A MoD for ViT Base at 12.5% capacity). |
| Q173 | The accuracy of DeiT-Tiny A-MoD improves from 0.97% to 52.6%... | FAIL | FAIL | 2412.20875v1.pdf (pdf_page 6, Table 1) | True failure. Hallucinated accuracy values (actual is +2.04 pp from 69.76% to 71.80%). |
| Q174 | No, DETR A-MoD does not achieve a higher mAP than standard DETR MoD across ev... | PASS | PASS | 2412.20875v1.pdf (pdf_page 8, Table 3) | Exact grounded premise refutation citing Table 3 capacity metrics. |
| Q175 | (a) DeiT-T (b) DeiT-S (c) ViT-B (d) ViT-L Figure 4: A-MoD... | FAIL | FAIL | 2412.20875v1.pdf (pdf_page 5) | True failure. Figure caption dump failing to name datasets or optimizer. |
| Q176 | A-MoD researchers compute the importance scores directly from the attention m... | FAIL | FAIL | 2412.20875v1.pdf (pdf_page 5) | True failure. Missed the core leave one out importance formulation. |
| Q177 | EA-series training compute complexity is O(tLD), training memory complexity i... | PASS | PASS | 2501.05730v1.pdf (pdf_page 1, pdf_page 5) | Exact complexities and parameter definitions from Table 1. |
| Q178 | Element-wise Attention Is All You Need Guoxin Feng 1 Abstract... | FAIL | FAIL | 2501.05730v1.pdf (pdf_page 1) | True failure. Dumped title and abstract header without explaining spikiness. |
| Q179 | EA generates D channel-level feature maps, while H-head self-attention genera... | PASS | PASS | 2501.05730v1.pdf (pdf_page 5) | Exact factual match to Section 3.4 comparison. |
| Q180 | No, it is not correct. The computation of EA-series is minimally affected by ... | FAIL | FAIL | 2501.05730v1.pdf (pdf_page 5) | True failure. Claimed training complexity is exponential in D instead of linear in L. |
| Q181 | The four stages of social learning that motivate the Retention Layer paper ar... | PASS | PASS | 2501.09166v1.pdf (pdf_page 1, pdf_page 2) | Exact four stages from Section 1. |
| Q182 | Unlike human cognition, which can encode and dynamically reorganize memories,... | FAIL | FAIL | 2501.09166v1.pdf (pdf_page 2) | True failure. Omitted Neural Turing Machine and Differentiable Neural Computer. |
| Q183 | c) Symbolic Storage of Behavior Instead of raw vector embeddings, the model c... | PASS | PASS | 2501.09166v1.pdf (pdf_page 4) | Exact symbolic storage types from Section 3. |
| Q184 | 𝐙(")=MHA(𝐗("))⟶𝐗(")+dropout(𝐙(")) (plus layer normalization). Retention Layer... | PASS | PASS | 2501.09166v1.pdf (pdf_page 6) | Correctly identifies m as pattern capacity/templates in memory matrix. |
| Q185 | National Strategy for Artificial Intelligence 93 managed National AI Marketpl... | PASS | PASS | National-Strategy-for-Artificial-Intelligence.pdf (pdf_page 8) | Correctly identifies the three marketplace activities from Chapter 3. |
| Q186 | National Strategy for Artificial Intelligence 85 Ethics of AI... | FAIL | FAIL | National-Strategy-for-Artificial-Intelligence.pdf (pdf_page 85) | True failure. Section header dump missing Fairness, Accountability, and Transparency. |
| Q187 | National Strategy for Artificial Intelligence 94 Financial implication of rec... | FAIL | FAIL | None (unanswerable / refusal) | True failure. Emitted excerpt from page 94 instead of issuing a refusal. |
| Q188 | The two papers that support caution about treating an attention heatmap as pr... | FAIL | FAIL | 2212.14776v3.pdf (pdf_page 1), 2412.20875v1.pdf (pdf_page 5) | True failure. Cited AI Strategy document instead of A MoD paper. |
| Q189 | No, the EA-series constant-length inference cost does not establish the same ... | PASS | PASS | 2501.05730v1.pdf (pdf_page 1), 2501.09166v1.pdf (pdf_page 1) | Grounded multi hop synthesis correctly distinguishing recurrent caching from persistent memory. |
| Q190 | I couldn't find any relevant passages in the workspace documents matching you... | FAIL | UNVERIFIED | SEC-Form-10Q.pdf (pdf_page 6), survay.csv (csv_row 44) | Unverified due to retrieval gap. Model refused because retrieval failed to supply survay.csv row 44 for cross document comparison. |
| Q191 | The interpretability paper demonstrates that while a Focus-Classify Attention... | PASS | PASS | 2212.14776v3.pdf (pdf_page 7), National-Strategy-for-Artificial-Intelligence.pdf (pdf_page 86) | Grounded multi hop synthesis connecting FCAM accuracy/interpretability gap to AI strategy goals. |
| Q192 | I couldn't find any relevant passages in the workspace documents matching you... | FAIL | UNVERIFIED | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 4), National-Strategy-for-Artificial-Intelligence.pdf (pdf_page 83) | Unverified due to retrieval gap. Model refused because cross document evidence linking NFHS and AI strategy was missing in context. |
| Q193 | 23,245 | FAIL | FAIL | SEC-Form-10Q.pdf (pdf_page 4) | True failure. Reported Services gross margin (23,245) instead of Services net sales (30,739). |
| Q194 | {"district": "North & Middle Andaman", "men_interviewed": 108} | PASS | PASS | NFHS_5_India_Districts_Factsheet_Data.xls (spreadsheet_row 3) | Exact JSON format and integer value from North & Middle Andaman row 3. |
| Q195 | I couldn't find any relevant passages in the workspace documents matching you... | PASS | PASS | None (unanswerable / refusal) | Grounded refusal. Benchmark figures for user's system are absent from the corpus. |

## Analysis of False Passes and False Failures

### False Passes (1 question)

* **Q142**: The question asked whether basic EPS of $2.03 million is the correct unit. The model pasted Note 3 from the 10-Q filing verbatim without refuting the premise or stating that the unit is per share. The premise correction contract awarded a pass because the table row header included the phrase `per share`. The answer is ungrounded in its premise correction and constitutes a false pass.

### False Failures (3 questions)

* **Q143**: The question asked for gross margin in dollar amount and percentage. The model gave $54,770 million and 50.1%, which are factually accurate values supported by page 19 of `SEC-Form-10Q.pdf`. The evaluator failed it because `content_match` expected 50.06% and formula tokens.
* **Q149**: The question asked for the percentage of South Andaman households with electricity. The model answered `99.58%`, matching Row 4 of `NFHS_5_India_Districts_Factsheet_Data.xls`. The evaluator failed it because single number answers lack the 25% token recall required across the English prompt prose.
* **Q157**: The question asked whether H05 in CC15 is zero because it is not numeric. The model answered `No, it is not correct. The value for H05 in 2025 Beverage and Tobacco Product Manufacturing (CC15) is 'C', which is not zero but a confidential code.` Supported by Row 499 of `survay.csv`. The evaluator failed it because it omitted the specific phrase `not a numeric`.

### Unverified Queries (2 questions)

* **Q190**: Comparing Apple's current ratio with survey All industries H36. Retrieval failed to bring `survay.csv` Row 44 into context, resulting in a fallback refusal.
* **Q192**: Assessing whether NFHS and AI strategy prove causal AI deployment in South Andaman. Multi hop cross document retrieval was incomplete, resulting in a fallback refusal.

## Reproduction of Q136 Contradictory Answer False Pass

In the benchmark runner, answer correctness is computed independently from citation validity.
Specifically, in `run_canonical_60_benchmark.py`:
1. The evaluator scores answer text via `content_match(ans_text, expected_ans)` and `compute_answer_relevancy(ans_text, expected_ans) >= 0.40`.
2. When testing contradictory answers like `The Commission File Number is definitely NOT 001-36743`, `content_match` evaluates to `True` (100% numeric overlap on `001-36743`) and `compute_answer_relevancy` scores `0.7600`, producing `is_correct = True`.
3. Citation validity is checked afterwards by `validate_citations`. Even when citations are completely invalid or ungrounded, `res_entry['is_correct']` is never modified or gated on `res_entry['citation_validity']['valid']`.
4. Furthermore, rule 3 in `validate_citations` requires only that at least one citation matches ground truth. In Q136, citation 1 was valid, so `validate_citations` reported `valid: True` despite citations 4 and 6 citing completely unrelated documents (`National-Strategy-for-Artificial-Intelligence.pdf` and `2212.14776v3.pdf`).

## Superseding Corrections for Locator References

This record supersedes informal row labels in previous reviews with authoritative locators from `canonical_60_ground_truth.json`:

* **Q154**: Ground truth locators are `survay.csv` rows `15` and `16` (All industries opening and closing stocks), not rows 2 and 3.
* **Q157**: Ground truth locator is `survay.csv` row `499` (Beverage and Tobacco CC15 H05 Value 'C'), not row 6.
* **Q160**: Ground truth locator is `rs_status_bill_passed_assent-1952-2016.csv` row `4` (The Anti-Hijacking Bill, 2016 milestones), not row 1.
* **Q161**: Ground truth locator is `rs_status_bill_passed_assent-1952-2016.csv` row `2` (Sikh Gurdwaras Bill, 2016), not row 3.
* **Q162**: Ground truth locator is `rs_status_bill_passed_assent-1952-2016.csv` row `4` (Anti-Hijacking Bill blank Committee cell), not row 1.
* **Q165**: Ground truth locator is `rs_status_bill_passed_assent-1952-2016.csv` row `4` (Anti-Hijacking Bill 2014 introduction year), not row 1.
* **Q144**: Ground truth locator is `NFHS_5_India_Districts_Factsheet_Data.xls` row `4` (South Andaman).
* **Q145**: Ground truth locators are `NFHS_5_India_Districts_Factsheet_Data.xls` rows `3` and `4`.
* **Q146**: Ground truth locators are `NFHS_5_India_Districts_Factsheet_Data.xls` rows `2`, `3`, and `4`.
* **Q148**: Ground truth locator is `NFHS_5_India_Districts_Factsheet_Data.xls` row `3`.
* **Q149**: Ground truth locator is `NFHS_5_India_Districts_Factsheet_Data.xls` row `4` (South Andaman row 4; retrieved rank was Rank 6, index 5).
* **Q163**: Ground truth locator is `rs_status_bill_passed_assent-1952-2016.csv` row `2` (Bill No. LIII; false matches removed were `row227` at rank 2 and `row206` at rank 5).
* **Q194**: Ground truth locator is `NFHS_5_India_Districts_Factsheet_Data.xls` row `3` (North & Middle Andaman).

## Artifact Hashes for External Review

Original run artifacts and audit files are available at the following paths with verified SHA-256 hashes:

* [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/manifest.json): `15b08c665fd2995159ccd73183aa5c49945a78c73b985b0416d092b70a9f5866`
* [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/query_details.json): `830669af6c44940e5ecb05dde5cb6f9b376f071db04c5baa52c1fe283b1f32a3`
* [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/summary.md](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/summary.md): `2c266aa9163eeea0bc9120fa121c1d1fb70af62383c8b38f1dded81de06102e2`
* [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_retrieval_rescore_followup.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_retrieval_rescore_followup.json): `6410683d186cd0fdc819e1241654e17244d692013b76ffecb3ca1eafa710d791`
* [backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_rescored_query_details.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/reports/a50c0a4f127c7418ffd79447430a9093d8b37f66/20261122_182354/offline_rescored_query_details.json): `a34fe3118ce78293b96a1638c1335a08a1320cdd9e94e9a4a5cbb4ece05a6fd4`
* [backend/evaluation_assets/canonical_60_ground_truth.json](file:///home/swyra/projects/knowledge-retrieval-engine/backend/evaluation_assets/canonical_60_ground_truth.json): `c0e89a4b3d7097c6cdb398a7d51f7cf0dfebb8fba2f71989b7c3defb823c5c96`
