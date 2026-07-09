# Data Transformation Agent — Pipeline Prompts Reference

This document consolidates every LLM prompt used across the extraction pipeline, as
implemented in code (source-of-truth, not the older `pipeline_prompts.pdf`). Dynamic
placeholders are shown in `{curly_braces}`.

## Pipeline flow

```
Schema setup
  ├─ from approved sample CSV  → deterministic (no LLM), see §1 for the human guide
  ├─ from documents            → Schema Inference (§2a)
  └─ from instructions         → Schema Inference (§2b)
        ↓
Sample Draft (--draft-only)    → fast path: analysis (§3) + draft extraction (§4, draft mode)
        ↓  [human approves the sample]
Full run:
  Pre-Extraction Analysis (§3)  extractor.py → _build_analysis_messages
        ↓
  Extraction (§4, full mode)    extractor.py → _build_extraction_messages
        ↓
  Transformation Review (§5)    validation/reviewer.py
        ↓
  Critic Semantic Audit (§6)    validation/critic.py
        ↓
  Final CSV append (+ optional final audit)
```

Model routing (via Portkey virtual keys, set in `.env`):
- **Full extraction / analysis / review / critic:** `anthropic.claude-opus-4-6` @ `@vertex-global-region`
- **Sample draft only:** `gemini-3-flash-preview` @ `@vertex-global-region` (`DRAFT_EXTRACTOR_MODEL`)

---

## 1. Sample CSV Drafting Guide

**Source:** `sample_csv_column_prompt.md` (repo root)
**Loaded by:** `extractor.py → load_sample_draft_guide()`, injected into the draft
extraction system prompt (§4, draft mode) under `=== SAMPLE DRAFTING GUIDE ===`.

This is the human-authored, per-column fill + row-formation guide (127 lines). It is the
reusable "sample generation prompt." Rather than duplicate its full text here, its exact
content lives in `sample_csv_column_prompt.md`; its key rules:

- **Generalization principle:** do not hardcode subject-specific assumptions; infer column
  semantics in order: (1) approved sample rows/contract, (2) source hierarchy, (3) global
  output-quality rules.
- **Column intent:** `source, grade_level, display_grade, grade_number, subject, domain,
  topic, l3, l4, l5, Display standard code, description, Standard code, czi_standard_code`
  with per-column fill rules (e.g. `grade_level` ∈ {Elementary School, Middle School, High
  School}; `Display standard code` unique, domain-prefixed on collision; `l3/l4/l5`,
  `Standard code`, `czi_standard_code` blank by default).
- **Row formation:** one row = one extractable standard/outcome; 6–10 rows for a draft;
  prefer consecutive rows from the same section.

---

## 2. Schema Inference

Used when there is no approved sample CSV. Returns a `TargetSchemaConfig`.
Both variants try Portkey first (`_infer_target_schema_via_portkey`), then Gemini
(`_infer_target_schema_via_gemini`). When using Portkey, this suffix is appended to the
user prompt: `Return a single JSON object that conforms to this TargetSchemaConfig JSON
schema: {TargetSchemaConfig.model_json_schema()}`.

### 2a. Schema inference from documents

**Source:** `batch_runner.py → _infer_document_schema` · **When:** `--infer-schema`

**System prompt:**
```
You design compact CSV schemas for semi-structured document extraction.
```

**User prompt:**
```
Infer a practical CSV schema for this document batch.
Return a TargetSchemaConfig with snake_case field names and human-friendly output_column values.
Prefer 4 to 8 fields. Avoid redundant metadata fields already captured separately.

Batch name: {batch_name}
Document snippets:
{document_snippets_json}
```

### 2b. Schema inference from instructions

**Source:** `batch_runner.py → _infer_instruction_schema` · **When:** `--instructions "..."`

**System prompt:**
```
You create practical CSV schemas from user extraction instructions.
```

**User prompt:**
```
Create a CSV extraction schema from the user's instructions.
Return a TargetSchemaConfig with:
- snake_case internal field names
- human-friendly output_column labels
- example_value when the instruction suggests one
- 3 to 10 focused fields

Batch name: {batch_name}
User instructions:
{instructions}

Optional document snippets:
{document_snippets_json}
```

---

## 3. Pre-Extraction Analysis (Source-to-Row Mapping)

**Source:** `extractor.py → _build_analysis_messages` · **Output schema:**
`PreExtractionUnderstanding`. Runs once per document (and once per draft).

**System prompt:**
```
You are an adaptive data transformation agent performing the pre-extraction understanding pass.
This artifact is the required plan the extractor will follow, so it must be thorough and complete.
Do not use static assumptions about layout.
Before extraction, inspect the unique source structure together with the sample CSV contract.
This step is only for understanding how rows are formed and how each column is derived; do not extract final values yet.
Do not hardcode subject-specific assumptions.
Infer the meaning of each output column for this subject from the approved sample contract first, then from the source structure.
You must account for the ENTIRE source: walk its table of contents and every body heading, and enumerate every section and sub-section so none is missed during extraction.
Curriculum sources repeat a row pattern many times (one row per benchmark/sub-item a, b, c, ...); estimate counts at that granularity, not per heading.
Preserve source notation exactly: identify mathematical symbols, equations, radicals, superscripts, subscripts, Greek letters, chemistry notation, and multilingual text that must survive verbatim into the output.
When the parsed markdown includes 'Parsed progression-matrix placement hints', treat those as authoritative for which CST/TS/S option tracks each benchmark belongs to.
grade_level must be exactly one of: Elementary School, Middle School, High School.
```

**User prompt:**
```
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

Validation feedback:
{correction_block}

Instructions:
1. Analyze this document's unique structure and summarize it in layout_analysis.
2. First infer what each output column means for this subject under the approved sample contract. Do not assume the same subject/domain/topic/grade pattern used by a different subject.
3. Explain in row_formation_logic how one complete output row is formed from this source and how that row pattern repeats across the source (identify the smallest repeating unit, e.g. each lettered/numbered benchmark).
4. For each schema field, explain in column_derivations what data it should contain and how it is derived from the source.
5. Determine whether values such as source, subject, domain, topic, grade_level, display_grade, and grade_number are document-level, section-level, or row-level for this specific subject and source. grade_level must always normalize to exactly one of Elementary School, Middle School, or High School.
6. If the approved sample implies canonical public source links, merged topic paths, row-specific stage labels, or transformed display codes, note that explicitly when supported by the source.
7. Build representative_row as a row-shaped preview showing what one complete row would contain under this sample contract.
8. List exclusion_rules describing what source content must be rejected from output rows.
9. Build section_inventory: walk the document's table of contents AND its body headings and list EVERY content section and sub-section, each with the approximate number of output rows it should yield (e.g. 'Algebra > Understanding dependency relationships: ~20 rows'). Do not omit any section, even short ones. This is the coverage checklist extraction must satisfy.
10. Set expected_total_rows to the sum of the per-section estimates in section_inventory — the total rows the full source should produce.
11. In coverage_expectations, call out sections or repeated sub-items that are easy to under-count or skip so they are not missed.
12. Fill notation_and_symbol_notes with the mathematical/scientific symbols, equations, radicals, superscripts, subscripts, Greek letters, and multilingual text present in the source that must be preserved verbatim, and flag anything that looks degraded in the parsed text.
13. Analyze the sample CSV Display standard code column for track patterns. Look for codes like S5.MAT.CST, S5.MAT.TS, S5.MAT.S where a track segment appears after the subject code for certain standards. Fill sample_csv_track_structure with notes explaining which standards have track suffixes and which do not.
14. When the source uses progression matrices with placement symbols (arrow, star, shaded box) beside option-track labels, read any legend in the document and any `Parsed progression-matrix placement hints` blocks. The hints auto-discover track labels from the page (e.g. CST/TS/S, Academic/Applied, or other vocabulary). Fill progression_matrix_legend with what each symbol means.
15. Map document track labels to sample CSV track segments in document_to_sample_track_mapping. E.g., if the document has CST and the sample uses .CST. in codes, create a mapping from CST to CST. If tracks are not present in either document or sample, leave empty.
16. Fill row_scope_rules with explicit scoping rules: a benchmark description applies ONLY to the option tracks where its placement symbol appears; never fan out the same description across all tracks unless each is marked. Encode display-code/track suffix rules based on the sample_csv_track_structure analysis.
17. Do not perform final cited extraction yet. This step is only for understanding the source-to-row mapping.

Source markdown:
{parsed_document.markdown}
```

---

## 4. Extraction

**Source:** `extractor.py → _build_extraction_messages` · **Output:** `ExtractionEnvelope`
(`anchoring_plan` + `payload_rows`). The same builder serves both full extraction and the
fast draft; `draft_max_rows` toggles draft mode.

**Base system prompt (both modes):**
```
You are an adaptive data transformation agent.
Do not use static assumptions about layout.
Use the pre-extraction understanding artifact as the required plan before mapping content into the provided schema.
The provided sample CSV contract defines the transformation rules and output style; follow it strictly.
Infer subject-specific column meaning from the sample contract and source rather than hardcoding one hierarchy interpretation across subjects.
grade_level must be exactly one of: Elementary School, Middle School, High School.
Description fields must preserve source meaning completely, without truncation, cross-row merges, neighbor contamination, or forced flattening when multiline structure is meaningful.
Preserve mathematical symbols, equations, radicals, superscripts, subscripts, chemistry notation, Greek letters, domain-specific notation, semantic punctuation, and multilingual text faithfully.
If notation looks degraded in native extraction, attempt a localized repair only when supported by the source.
Reject noise such as headers, footers, page numbers, continuation fragments, appendix-only noise, N.B. notes, layout labels, and extraction artifacts unless the contract explicitly requires them.
When row_scope_rules or parsed progression-matrix placement hints are present, emit one output row per (benchmark, marked option track) pair only — never duplicate a benchmark across CST, TS, and S unless each track is explicitly marked.
[citation rules — only when EXTRACTION_CITATIONS is enabled:]
  Citations must be verbatim snippets copied from the source markdown. If a value is absent, return null for the value and an empty string for its citation.
Keep anchoring_plan short and field-specific.
Extract every valid row supported by the source and approved sample contract.
If the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic cell using ' | ' rather than duplicating the row only for topic labels.
If the approved sample supports prefixed or formed display codes and the same raw display code repeats, prefix it with a short domain code or topic code plus a dot when that disambiguation is supported by the source structure.
Do not stop after the first row.
Do not omit valid domains, topics, or descriptions that belong in output.
Return payload_rows in source order with no duplicates and no missing supported rows.
grade_level must be exactly one of: Elementary School, Middle School, High School.
```

**Draft-mode system prompt additions** (appended only when `draft_max_rows` is set):
```
You are producing a SHORT SAMPLE DRAFT, not a full extraction.
Produce at most {draft_max_rows} representative, consecutive rows from the SAME section so a human can approve the transformation pattern before the full run. Do NOT attempt exhaustive coverage.
Follow the sample-drafting guide below for exact per-column fill rules and row-formation rules.

=== SAMPLE DRAFTING GUIDE ===
{contents of sample_csv_column_prompt.md — see §1}

CRITICAL - PRESERVE SOURCE CASING: Copy every value verbatim from the source, keeping the ORIGINAL capitalization exactly as written. Do NOT lowercase, uppercase, title-case, or otherwise normalize letter case in description, topic, domain, subject, codes, or any other field. If the source says 'In a predator-prey relationship', return 'In a predator-prey relationship' — never 'in a predator-prey relationship'.
```

**User prompt:**
```
Schema to populate:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Document metadata:
- source_name: {parsed_document.source_name}
- source_type: {parsed_document.source_type}
- source_path: {parsed_document.source_path}

{chunk_note (if chunked)}Validation feedback:
{correction_block}

Instructions:
1. Use the pre-extraction understanding artifact before extracting any values.
2. Build a temporary field anchoring plan explaining where each schema field appears in this specific layout.
3. Apply the sample-derived meaning of each column consistently across all rows. Once you infer what source, subject, domain, topic, grade_level, display_grade, and grade_number mean for this subject, do not drift to a different interpretation. grade_level must be exactly Elementary School, Middle School, or High School.
4. [FULL MODE] Extract every valid output row from the source, not just one row.
   [DRAFT MODE] Produce clean, representative rows for the sample draft (see item 5); do not attempt full coverage.
5. [FULL MODE] CRITICAL - EXHAUSTIVE EXTRACTION REQUIRED: This is NOT a sampling task. You MUST produce rows for EVERY SINGLE benchmark item in the source document. The pre-extraction analysis identified expected_total_rows as the target count. Your extraction MUST approach that count. Treat section_inventory as a mandatory checklist - produce rows for EVERY section and sub-section it lists, at the row granularity given in row_formation_logic (one row per benchmark/sub-item). Do not skip, summarize, truncate, or collapse sub-items. Do not stop after a few examples. If you produce significantly fewer rows than expected_total_rows (e.g., only 10-20 rows when 500+ are expected), you have FAILED this extraction task. Work systematically through the entire source document section by section until all content is extracted.
   [DRAFT MODE] SAMPLE DRAFT MODE: Produce at most {draft_max_rows} representative rows drawn from one coherent section (prefer consecutive rows), enough to make the transformation pattern clear. Do NOT extract the whole document and do NOT aim for expected_total_rows here — this is a preview.
6. Return payload_rows in source reading order with one object per output row.
7. If the approved sample supports canonical public source links, use those links when they are identifiable from the source documents or staging context instead of local file names.
8. If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels.
9. If the approved sample supports prefixed or formed display codes and the same raw display standard code repeats, disambiguate it with a short domain code or topic code prefix and a dot when that is needed to keep `Display standard code` unique.
10. If the source contradicts the draft representative_row, follow the source while preserving the sample contract.
11. Honor progression_matrix_legend, row_scope_rules, and document_to_sample_track_mapping from the pre-extraction artifact. When parsed placement hints list `applies_to` and `not_for` tracks for a benchmark, create rows only for `applies_to` tracks and skip `not_for` tracks entirely. Use document_to_sample_track_mapping to translate document track labels (e.g., 'CST') into the correct sample CSV display code segments (e.g., '.CST.') when forming Display standard code values.

Source markdown:
{source_markdown}
```

**Region-targeting locate pass** (`extractor.py → _locate_extraction_region`, runs before
chunking to bound extraction to the outcomes section):

*System:*
```
You locate the region of a curriculum document that contains the extractable rows (typically the syllabus outcomes/standards), so downstream extraction can skip front matter, rationale, assessment guidance, glossary, appendices, and sample work. Return anchors as VERBATIM text copied from the provided outline. For paginated sources use the '# Page N' markers; otherwise use heading lines. Return your response as a single JSON object.
```

*User:*
```
The approved sample CSV contract describes what one extractable row looks like:
{sample_contract_json}

Below is the structural outline (headings and page markers) of the document '{source_name}'.
Identify:
- start_anchor: the verbatim outline line where extractable content begins.
- end_anchor: the verbatim outline line where extractable content ends (empty to run to the end).
- skip_anchors: verbatim outline lines between start and end whose sections must be skipped.
- confidence: high, medium, or low.

If you cannot confidently locate an outcomes region, return empty anchors with confidence=low.

Document outline:
{outline}
```

---

## 5. Transformation Review (evaluate-and-fix)

**Source:** `validation/reviewer.py`. The pipeline uses the **merged evaluate-and-fix**
builder by default (one call per batch fixes rows and decides validity); the separate
single-row and batch-review builders are fallbacks. Prompts below are the merged variant.

**System prompt:**
```
You are a transformation evaluator-and-fixer. In ONE step you both fix each extracted CSV row against the source and decide whether it is valid.
Apply only minimal, source-supported fixes; do not invent facts.
Preserve citations, symbols, multilingual content, multiline structure, and row boundaries.
Infer subject-specific column meaning from the sample contract and source; do not hardcode one hierarchy across subjects. grade_level must be exactly one of: Elementary School, Middle School, High School.
Review each row INDEPENDENTLY; do not merge, drop, or reorder rows, and return exactly one item per input row echoing its row_index.
For each row, put in corrected_row your best source-faithful version, and in remaining_issues ONLY concrete, demonstrable defects you could NOT fix from the source (reasons the row should be rejected). If the row is good after your fixes, remaining_issues must be empty. Default to keeping rows: transformation, mapping, inheritance, and synthesis are expected and correct, not defects. Do not reject on speculation or vague doubt.
Return your response as a single JSON object.
```

**User prompt:**
```
Schema:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Rows to evaluate and fix (each has a row_index):
{rows_json}

Fix goals (apply to EACH row from the source):
- make subject, domain, topic, grade_level, display_grade, grade_number/grade_string, and source follow the sample-derived column semantics for this subject
- populate document-level and section-level inherited fields (e.g. subject, domain, topic, l3) on EVERY row from the plan's column derivations, even when they are not printed next to each benchmark
- normalize grade_level to exactly Elementary School, Middle School, or High School when the source supports it
- if grade_number/grade_string is a numeric range like `9-12`, expand to `9,10,11,12` (no spaces after commas)
- if the sample supports merged topic cells and one standard spans multiple topics, merge topic names with ` | ` rather than duplicating rows
- follow the sample's display standard code style (source-faithful or synthetic/derived); keep it unique, adding a short domain/topic prefix with a dot when the same raw code repeats
- preserve description completeness, notation, bullet/punctuation style, and multiline behavior required by the sample
- if a row carries `must_fix_issues`, those are defects a prior pass found: fix EACH from the source and do not reintroduce them

Reject a row (list it in remaining_issues) ONLY for concrete problems:
- a required column is genuinely absent from the source and cannot be derived
- a value is copied from the wrong place (neighboring-row contamination) or is document noise (headers, footers, page numbers, nav links)
- a description is truncated mid-sentence or drops required sub-parts
- loss/garbling of math, chemistry, Greek, or multilingual characters the source clearly contains
- grade_level is not one of the three allowed values
- a value plainly contradicts the sample contract

Output requirements:
- return one item per input row in `items`, each with the matching row_index
- corrected_row = the final, fixed row
- review.was_modified true only if you changed the row; review.fixes_applied lists only real corrections
- remaining_issues = ONLY unfixable, concrete defects (empty if the row is valid)

Original document markdown:
{parsed_document.markdown}
```

*Fallback builders* `_build_review_messages` (single row) and `_build_batch_review_messages`
(batch) use the same rubric but only fix (no validity decision); the pipeline falls back to
them if the merged call fails.

---

## 6. Critic Semantic Audit

**Source:** `validation/critic.py`. Single-row (`_build_audit_messages`) and batch
(`_build_batch_audit_messages`) variants share the same rubric; batch version below.

**System prompt:**
```
You are a data extraction validator. Your goal is to pass every row that is a faithful, contract-consistent transformation of the source, and to reject only rows with a concrete, demonstrable defect you can point to. Transformation of source text into contract values is expected and correct, not a defect. Judge each row independently and return one verdict per row. When in doubt, return VALID. Return your response as a single JSON object.
```

**User prompt:**
```
Audit each extracted row against the original document and the approved sample CSV contract.
Judge every row INDEPENDENTLY and return exactly one verdict per row, echoing its row_index.

Default to VALID. This pipeline's job is to TRANSFORM messy source text into clean contract values, so transformation, mapping, inheritance, and synthesis are EXPECTED and correct — not suspicious. Return tag=INVALID for a row only when you can point to a CONCRETE, demonstrable violation (quote the offending value and say exactly which rule it breaks). Do NOT reject on speculation, hedging, or vague doubt ("possible", "may have", "risks", "insufficient clarity", "lacks explicit evidence" are NOT valid reasons). If you are not sure a row is wrong, it is VALID.
{citation guidance block — only when citations enabled}

Reject a row ONLY for these concrete problems:
{citation reject bullets — only when citations enabled}
- A field value is clearly copied from the wrong place (neighboring-row contamination) or is document noise (headers, footers, page numbers, N.B. notes, nav links, layout labels).
- grade_level is not exactly one of: Elementary School, Middle School, High School.
- A value plainly contradicts the sample CSV contract's stated rules.

{citation do-not-reject clause — only when citations enabled}

Sample CSV transformation contract:
{sample_contract_json}

Extracted rows to audit (each has a row_index):
{rows_json}

Original document markdown:
{parsed_document.markdown}
```

---

## Notes

- Every LLM call goes through the shared Instructor client (`portkey_client.call_portkey_structured`)
  so responses are validated against a Pydantic schema on the wire.
- `{correction_block}` carries prior validation failures back into analysis/extraction on retry.
- Citations are optional (`EXTRACTION_CITATIONS`); citation-specific instructions appear only
  when enabled.
- Rows are stamped with `source` and pipeline metadata AFTER review/critic, and hidden from
  review via `schema_only_row_view` so the LLM does not flag pipeline fields as violations.
