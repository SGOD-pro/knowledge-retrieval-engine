# Review, re-factor, 2026-11-17

**Reviewed by**: gemini-2.5-pro (author on claude-3-5-sonnet)
**Scope**: 11 files, branch vs af02997 (plus working tree changes)
**Verdict**: Approve

## Summary
This follow-up review evaluates the resolution of 3 Major and 4 Minor findings previously identified in the Spec 0007 Slice 2 implementation (`re-factor` branch vs `af02997`). The revisions systematically eliminate grounding brittleness by introducing boundary-aware sentence segmentation that respects corporate abbreviations (`Inc.`, `Corp.`, `Ltd.`) and numeric decimals (`51.5%`, `$3.5M`), disambiguating the calendar month May from epistemic modality, expanding active syntactic binding allowlists to accommodate descriptive role nouns and temporal adverbs, and localizing negation detection to prevent false rejections on non-negating prepositional phrases. Distributed budget fencing, lease releases on storage errors, and page number boundary safety have also been verified, and the comprehensive 64-test suite in `test_knowledge_graph_slice2.py` fully validates the entire extraction and persistence surface.

## Strengths
- **Robust Boundary-Aware Sentence Segmentation**: `split_sentences_with_spans` elegantly handles decimal numbers and honorifics while disambiguating corporate suffixes based on clause verb presence and capitalized boundary tokens, cleanly resolving abbreviation-induced grounding dropouts.
- **Accurate Calendar Month vs Modal Auxiliary Disambiguation**: `_has_unsupported_modality` cleanly distinguishes temporal and date context (prepositional frames like "in/during/since" and date numbers like "2023", "May 15") from epistemic modal constructions ("may acquire", "may have acquired"), preserving factual historical claims without relaxing modality guards.
- **Localized Negation & Syntax Flexibility**: Replacing sentence-wide negation checks with `_has_predicate_negation` and expanding intervening token sets allows natural business expressions ("with no regulatory objections", "competitor", "supplier", "in 2023") to ground accurately while continuing to strictly reject reversed voice and verbal negation.
- **Fail-Closed Staging Resilience & Monotonic Lease Release**: Wrapping relationship staging in try/except blocks and enabling monotonic worker lease releases in `fail_extraction_budget` across InMemory, SQLite WAL, and DynamoDB ensures lease capacity is immediately recovered without risking race conditions against newer workers.

## Test coverage
Test coverage is comprehensive and robust. The 64 tests in `backend/tests/test_knowledge_graph_slice2.py` pass without failures, including targeted regression tests for corporate abbreviations (`Inc.`, `Corp.`, `Ltd.`), financial percentages, calendar month May vs modal verbs, descriptive business nouns, temporal adverbs, modal passive voice with auxiliary `be`, page number 0 clamping on `DocumentLocation`, staging error recovery in the budget ledger, non-negating prepositional phrases, and dictionary chunk provenance.
