# Data Transformation Agent — Complete Pipeline Prompts

This document consolidates every LLM prompt and prompt-adjacent instruction used across the extraction pipeline. Dynamic placeholders are shown in `{curly_braces}`.

---

## Pipeline Flow

```
Sample CSV / Schema Setup
        ↓
Pre-Extraction Analysis  (agent_engine.py)
        ↓
Extraction               (agent_engine.py)
        ↓
Transformation Review    (validation/reviewer.py)
        ↓
Critic Semantic Audit    (validation/critic.py)
        ↓
Final CSV append (VALID rows only)

Separate path: CSV Audit Mode (csv_audit.py) — audits an already-extracted CSV
```

---

## 1. Sample CSV Drafting Guide

**Source:** `sample_csv_column_prompt.md`  
**Used by:** Chat intake, Codex UI worker, schema contract derivation

This is the human-facing prompt for drafting an approved sample CSV before full extraction.

### Generalization Principle

Do not hardcode subject-specific assumptions such as:

- what `subject` must contain
- whether `domain` is the syllabus name, strand name, or course family
- whether `topic` is a single heading or a merged topic path
- whether `grade_level` should be `Middle School`, `High School`, `Senior Years`, or another band
- whether `display_grade` and `grade_number` are document-level or row-level values
- whether `source` should be a local file name, canonical public URL, PDF URL, or row-specific webpage URL

Instead, infer the column semantics for the current run in this order:

1. approved sample CSV rows or sample contract
2. source document hierarchy and repeated row structure
3. stable global output-quality rules

The sample contract defines how columns should behave for this subject. The source document defines which concrete values belong in those columns for this run.

### Reusable Prompt

Create a sample CSV using this exact column intent:

`source, grade_level, display_grade, grade_number, subject, domain, topic, l3, l4, l5, Display standard code, description, Standard code, czi_standard_code`

Fill the columns as follows:

- `source`: Put the exact source URL that supports that specific row. If a subject uses multiple source links, rows from different documents or webpages may have different `source` values within the same CSV. Do not force a single common source when the rows actually come from multiple links.
- `grade_level`: Use the broad schooling band shown or implied by the source, such as `Elementary School`, `Middle School`, `High School`, `Middle Years`, `Senior Years`, `Secondary`, or a similar official grouping.
- `display_grade`: Use the grade, stage, or displayed learner band exactly as it should appear to a reader, such as `K`, `1`, `9`, `Stage 4`, `Life Skills for Stage 4/5`, or `9-12`.
- `grade_number`: Use the sortable/internal grade value. If the source uses stages instead of grades, keep the stage label. If the source uses a numeric grade band such as `9-12`, expand it to `9,10,11,12` instead of keeping the range as a single token. Do not put spaces after commas.
- `subject`: Use the main subject area or official parent subject name.
- `domain`: Use the main subdivision under the subject. This should be a meaningful named grouping from the source, such as a discipline, framework section, or course-level grouping. Do not use a code here.
- `topic`: Use the heading directly under `domain` for the row. This should be the actual named heading from the source, such as a strand, unit, organizer, or named cluster heading. Do not put generic labels like `Cluster 1.1` if the cluster has a real heading such as `Use of linguistic elements`. If the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one cell using ` | `, for example `Topic A | Topic B`.
- `l3`: Leave blank during sample preparation unless the user explicitly asks for this hierarchy level to be filled.
- `l4`: Leave blank during sample preparation unless the user explicitly asks for this hierarchy level to be filled.
- `l5`: Leave blank during sample preparation unless the user explicitly asks for this hierarchy level to be filled.
- `Display standard code`: Put the visible code that should be shown in the sample row. This code may be taken directly from the source or formed from the source structure and sample pattern when needed. It does not have to be explicitly printed in the source, as long as the constructed code is consistent, defensible, and aligned with the approved sample style.
  `Display standard code` must be unique per row and must never be duplicated within the same CSV.
  If the same source code appears in multiple places in the document, make it unique by prefixing a two-letter or three-letter domain code and a dot. In that case, use the structure `domaincode.originalcode`, for example `LC.1.1` or `CA.1.1`.
  If the approved sample supports prefixed or formed display codes, and repeated rows share the same description and the same raw display code, prefix a short domain code or topic code and a dot, whichever makes the display code unique, for example `DA.1.1` or `TOP.1.1`.
  If the source uses bullets instead of numbered codes, assign numeric sequence values to those bullets for `Display standard code` so each bullet becomes countable and unique.
- `description`: Put the full standard, outcome, or expectation text for that row. Keep the meaning complete. Do not shorten unless the source itself is short. If the source description is multiline, preserve the full content and do not truncate it. If the source uses a parent-child pattern and the child item depends on the parent text for full meaning, merge the parent and child text into the same description cell.
- `Standard code`: Leave blank during sample preparation unless the user explicitly instructs that this column should be filled.
- `czi_standard_code`: Leave blank unless the user explicitly provides or requests values for it.

### Row Formation Rules

- One CSV row should represent one extractable standard, outcome, descriptor, or expectation statement.
- A draft or prepared sample CSV should usually contain `6` to `10` proper data rows so the transformation pattern is clear before full extraction.
- Each row must carry the source link for the exact document or webpage that supports that row.
- Sample rows should be proper rows from the source, not scattered examples chosen from unrelated parts unless the user asks for variety.
- Prefer consecutive rows from the same section when preparing a sample.
- Use official named headings wherever possible.
- Keep `l3`, `l4`, and `l5` blank by default in sample CSVs.
- Keep `Standard code` blank by default in sample CSVs.
- Ensure `Display standard code` is unique across all rows in the CSV.
- If a source code repeats across domains or sections, resolve the duplication by adding a two-letter or three-letter domain code prefix to the display code.
- If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one `topic` cell using ` | ` rather than duplicating the row only to repeat the same standard.
- If the approved sample supports prefixed or formed display codes and repeated rows share the same description and the same raw display code, use a short domain code or topic code prefix with a dot, whichever produces a unique `Display standard code`.
- If a section uses bullets rather than numbered items, number the bullets in reading order and use those numbers to form `Display standard code`.
- Do not invent hierarchy values just because the source has multiple levels.
- Do not fill columns with code labels, cluster numbers, or structural markers when a real heading name is available.
- If a parent heading and child bullet together form the real meaning of a row, merge them into one description rather than leaving the child fragment unsupported.

### Column-Semantics Inference Rules

Before drafting sample rows or running full extraction, determine the meaning of every output column for the current subject and source.

- Infer `source` format from the approved sample when available. Prefer canonical public source links over local file names when the public source can be identified reliably from the staged source or document metadata.
- Infer `grade_level` from the sample's schooling-band style first, then from the source's official banding terminology.
- Infer `display_grade` from the row-level or section-level learner/stage label that the sample pattern expects. Do not force one document-wide value if the source clearly contains row-specific stage bands such as `Stage 4`, `Stage 5`, and `Life Skills`.
- Infer `grade_number` from the sample's normalization style. If the sample keeps stage labels, keep stage labels. If the sample expands grade ranges, expand them consistently.
- Infer `subject` from the approved sample's scope. In some subjects it may be the parent learning area; in others it may be the course title itself. Do not assume one universal pattern.
- Infer `domain` from the approved sample's placement pattern. It may hold the syllabus/framework title, a strand family, a discipline name, or another meaningful grouping. Do not force `domain` to always be the same hierarchy level across subjects.
- Infer `topic` from the approved sample's placement pattern. It may be a single heading, an organizer, a merged path, or a focus-area path joined with ` | `.
- Infer whether `Display standard code` should be copied exactly from the source, slightly normalized, or synthetically formed according to the approved sample style.
- Infer whether `Standard code` should remain blank, mirror another code, or carry a second code system only when the user or sample contract requires that behavior.
- Infer whether optional hierarchy columns should stay blank or be populated from the source only when the approved sample contract supports those levels.

When the sample and source disagree about hierarchy labels, prioritize preserving the approved sample's column semantics while still using source-supported content.

### Mapping Guidance By Pattern

- If the source has `subject > domain/discipline > strand/topic > coded outcomes`, map those directly to `subject`, `domain`, and `topic`.
- If the source is a single framework document with named domains and named cluster headings, use the domain name in `domain` and the cluster heading text in `topic`.
- If the source is a course with units and outcomes, use the course or framework name in `domain` and the unit heading in `topic`.

### Prompt Workflow

For every subject, perform the prompt reasoning in this order:

1. Read the approved sample contract or approved sample rows and infer what each output column means for this subject.
2. Read the source and identify the repeated row unit, hierarchy labels, and noise to exclude.
3. Write a source-to-row mapping that explains how one complete row is formed.
4. Draft or extract rows only after that mapping is established.
5. Review whether the resulting rows still match the sample-derived column semantics.

### Output Quality Rules

- Preserve capitalization and wording faithfully, except for minor cleanup of OCR or line-break noise.
- Ensure complete coverage of all valid source-supported rows that match the approved sample contract. Do not miss a supported domain, topic, description, or standard-level row.
- Do not create duplicate rows for the same source-supported standard unless the approved sample contract clearly requires that structure.
- If a description spans multiple lines in the source, preserve all of its content in the CSV cell. Do not drop later lines just to make the row shorter.
- When merging parent and child text, preserve the source meaning and relationship clearly. Do not merge unrelated sibling items together.
- Keep `Display standard code` unique across the full CSV. Do not allow duplicate `Display standard code` values in the final output.
- If the approved sample supports prefixed or formed display codes, use that same sample-aligned disambiguation style consistently when a repeated raw code must be made unique.
- If the approved sample supports merged topic cells, use the same sample-aligned `topic` merge style consistently, including the ` | ` separator.
- Preserve mathematical symbols and notation exactly where possible, including operators, inequalities, exponents, subscripts, radicals, fractions, set notation, Greek letters, and other subject-specific symbols. Do not simplify or replace them with incorrect plain-text approximations.
- If the source or sample is in a language other than English, preserve the original language, script, accents, and diacritics exactly where possible. Do not translate, normalize, or anglicize the text unless the user explicitly asks for translation.
- Do not include page headers, footers, page numbers, appendix labels, or continuation fragments unless they are part of the actual standard text.
- Use the sample CSV contract consistently across all rows.
- Use sample-derived column meaning consistently across all rows. Once `subject`, `domain`, `topic`, `grade_level`, `display_grade`, and `grade_number` semantics have been inferred for the subject, do not drift to a different interpretation in later rows.
- Do not move content into the wrong column. Domain, topic, description, and display-code placement must remain aligned with the approved sample.
- Do not truncate, flatten incorrectly, contaminate with neighboring rows, or silently drop meaningful sub-parts from descriptions.
- If a column is not clearly supported by the source and the user has not asked for a derived value, leave it blank.
- If the subject is compiled from multiple links, keep all other column rules the same and only vary `source` row by row as needed.

---

## 2. Schema Inference from Documents

**Source:** `chat_batches.py` → `_infer_schema_with_gemini`  
**When:** `--infer-schema` without an approved sample CSV

### System Prompt

```
You design compact CSV schemas for semi-structured document extraction.
```

### User Prompt

```
Infer a practical CSV schema for this document batch.
Return a TargetSchemaConfig with snake_case field names and human-friendly output_column values.
Prefer 4 to 8 fields. Avoid redundant metadata fields already captured separately.

Batch name: {batch_name}
Document snippets:
{document_snippets_json}
```

**Note:** When using Portkey, an additional suffix is appended:

```
Return a single JSON object that conforms to this TargetSchemaConfig JSON schema:
{TargetSchemaConfig.model_json_schema()}
```

---

## 3. Schema Inference from Instructions

**Source:** `chat_batches.py` → `_infer_schema_from_instructions_with_gemini`  
**When:** `--instructions "..."` without an approved sample CSV

### System Prompt

```
You create practical CSV schemas from user extraction instructions.
```

### User Prompt

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

## 4. Pre-Extraction Analysis (Source-to-Row Mapping)

**Source:** `agent_engine.py` → `_build_analysis_messages`  
**Model:** Extractor model (Gemini via Instructor, or Portkey)  
**Output schema:** `PreExtractionUnderstanding`

### System Prompt

```
You are an adaptive data transformation agent. Do not use static assumptions about layout. Before extraction, inspect the unique source structure together with the sample CSV contract. This step is only for understanding how rows are formed and how each column is derived. Do not hardcode subject-specific assumptions. Infer the meaning of each output column for this subject from the approved sample contract first, then from the source structure.
```

### User Prompt

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
3. Explain in row_formation_logic how one complete output row is formed from this source and how that row pattern repeats across the source.
4. For each schema field, explain in column_derivations what data it should contain and how it is derived from the source.
5. Determine whether values such as source, subject, domain, topic, grade_level, display_grade, and grade_number are document-level, section-level, or row-level for this specific subject and source.
6. If the approved sample implies canonical public source links, merged topic paths, row-specific stage labels, or transformed display codes, note that explicitly when supported by the source.
7. Build representative_row as a row-shaped preview showing what one complete row would contain under this sample contract.
8. List exclusion_rules describing what source content must be rejected from output rows.
9. In coverage_expectations, identify what domains, topics, sections, or repeated row items must be captured so valid source content is not missed.
10. Do not perform final cited extraction yet. This step is only for understanding the source-to-row mapping.

Source markdown:
{parsed_document.markdown}
```

### Dynamic Variables

| Variable | Content |
|---|---|
| `{schema_json}` | Full `TargetSchemaConfig` JSON |
| `{sample_contract_json}` | `SampleTransformationContract` JSON, or `"null"` |
| `{correction_block}` | `"No prior validation failures.\n"` or `"Previous validation failure log. Correct these exact issues:\n{prior_error_log}\n"` |

---

## 5. Extraction

**Source:** `agent_engine.py` → `_build_extraction_messages`  
**Model:** Extractor model (Gemini via Instructor, or Portkey)  
**Output schema:** `ExtractionEnvelope` (anchoring_plan + payload_rows)

### System Prompt

```
You are an adaptive data transformation agent. Do not use static assumptions about layout. Use the pre-extraction understanding artifact as the required plan before mapping content into the provided schema. The provided sample CSV contract defines the transformation rules and output style; follow it strictly. Infer subject-specific column meaning from the sample contract and source rather than hardcoding one hierarchy interpretation across subjects. Description fields must preserve source meaning completely, without truncation, cross-row merges, neighbor contamination, or forced flattening when multiline structure is meaningful. Preserve mathematical symbols, equations, radicals, superscripts, subscripts, chemistry notation, Greek letters, domain-specific notation, semantic punctuation, and multilingual text faithfully. If notation looks degraded in native extraction, attempt a localized repair only when supported by the source. Reject noise such as headers, footers, page numbers, continuation fragments, appendix-only noise, N.B. notes, layout labels, and extraction artifacts unless the contract explicitly requires them. Citations must be verbatim snippets copied from the source markdown. If a value is absent, return null for the value and an empty string for its citation. Keep anchoring_plan short and field-specific. Extract every valid row supported by the source and approved sample contract. If the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic cell using ' | ' rather than duplicating the row only for topic labels. If the approved sample supports prefixed or formed display codes and the same raw display code repeats, prefix it with a short domain code or topic code plus a dot when that disambiguation is supported by the source structure. Do not stop after the first row. Do not omit valid domains, topics, or descriptions that belong in output. Return payload_rows in source order with no duplicates and no missing supported rows.
```

### User Prompt

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

Validation feedback:
{correction_block}

Instructions:
1. Use the pre-extraction understanding artifact before extracting any values.
2. Build a temporary field anchoring plan explaining where each schema field appears in this specific layout.
3. Apply the sample-derived meaning of each column consistently across all rows. Once you infer what source, subject, domain, topic, grade_level, display_grade, and grade_number mean for this subject, do not drift to a different interpretation.
4. Extract every valid output row from the source, not just one row.
5. Ensure complete source coverage for all valid domains, topics, and descriptions that match the sample contract.
6. Return payload_rows in source reading order with one object per output row.
7. If the approved sample supports canonical public source links, use those links when they are identifiable from the source documents or staging context instead of local file names.
8. If the approved sample supports merged topic cells and one standard genuinely spans multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels.
9. If the approved sample supports prefixed or formed display codes and the same raw display standard code repeats, disambiguate it with a short domain code or topic code prefix and a dot when that is needed to keep `Display standard code` unique.
10. If the source contradicts the draft representative_row, follow the source while preserving the sample contract.

Source markdown:
{parsed_document.markdown}
```

### Schema Field Descriptions (Instructor constraints)

Each extraction field carries a Pydantic `Field(description=...)` from `schema_config.json`. Each value field also has a sister `{field}_source_citation` field with description:

```
Verbatim supporting quote for '{field_name}' from the source document.
```

---

## 6. Transformation Review & Fix

**Source:** `validation/reviewer.py` → `_build_review_messages`  
**Model:** Critic model (OpenAI, Anthropic, or Portkey)  
**Output schema:** `TransformationReviewEnvelope` (review metadata + corrected_row)

### System Prompt

```
You are a transformation evaluator and fixer. Review extracted CSV rows against the source document, the sample CSV contract, and the row-formation plan. Apply only minimal supported fixes. Do not invent missing facts. Preserve citations, symbols, multilingual content, multiline structure, and row boundaries. Do not hardcode one hierarchy interpretation across subjects; preserve the subject-specific column meanings inferred from the sample contract and source. Return your response as a single JSON object.
```

### User Prompt

```
Schema:
{schema_json}

Sample CSV transformation contract:
{sample_contract_json}

Pre-extraction understanding artifact:
{planning_json}

Current transformed row:
{row_json}

Review goals:
- check whether the transformation matches the source and the sample contract
- check whether subject, domain, topic, grade_level, display_grade, grade_number, and source follow the sample-derived column semantics for this subject rather than a generic cross-subject assumption
- fix incorrect placement, formatting, merged/split text issues, or minor transformation errors when the source supports a correction
- if the approved sample pattern implies a canonical public source link and the source supports identifying it, prefer that canonical link over a local staged file name
- if the approved sample pattern implies row-specific stage or learner-band values, do not collapse them into one document-wide grade label
- if grade_number is a numeric range such as `9-12`, normalize it to a comma-separated sequence such as `9,10,11,12`, with no spaces after commas
- if the approved sample supports merged topic cells and one standard genuinely applies to multiple topics, merge those topic names into one topic field using ` | ` rather than duplicating the row only for topic labels
- ensure `Display standard code` stays unique within the CSV; if the same raw code repeats across multiple domains or sections, add a short domain code prefix when the source structure supports that disambiguation
- if the approved sample supports prefixed or formed display codes and repeated rows share the same description and raw display code, use a short domain code or topic code prefix with a dot, whichever makes the display code unique
- preserve display logic such as synthetic or source-faithful display standard code when consistent with the sample contract
- preserve description completeness, notation, bullets/punctuation style, and multiline behavior required by the sample
- reject noise, neighboring-row contamination, and unsupported values

Output requirements:
- review.was_modified should be true only if you actually change the row
- review.issues_found should list all detected transformation problems
- review.fixes_applied should describe only the corrections you actually made
- corrected_row must be the final row to send to the final critic

Original document markdown:
{parsed_document.markdown}
```

### Programmatic Pre-Fixes (non-LLM)

Before the LLM review, the reviewer applies:

- Trim surrounding whitespace on string fields
- Expand `grade_number` ranges like `9-12` → `9,10,11,12`
- Normalize pipe-joined `topic` values (dedupe segments, standardize ` | ` separator)

---

## 7. Critic Semantic Audit

**Source:** `validation/critic.py` → `_build_audit_messages`  
**Model:** Critic model (OpenAI, Anthropic, or Portkey)  
**Output schema:** `SemanticAuditVerdict` (tag: VALID | INVALID)

### System Prompt

```
You are an adversarial data extraction critic. Be skeptical, precise, and strict about style-preserving transformations. Return your response as a single JSON object.
```

### User Prompt

```
Audit the extracted row against the original document markdown and the approved sample CSV contract.

Validation rules:
- Reject any field whose citation is not verbatim or does not support the field value.
- Reject fields that are semantically irrelevant to the document.
- Reject rows where important field meanings are mismatched.
- Reject rows whose transformed values or field placement violate the sample CSV contract.
- Reject rows that use a generic cross-subject interpretation of subject, domain, topic, source, grade_level, display_grade, or grade_number when the approved sample contract and source support a more specific subject-aligned mapping.
- Reject local file-name `source` values when the approved sample contract clearly expects canonical public source links and those links are identifiable from the source or staging context.
- Reject document-wide grade labels when the approved sample contract and source support row-specific stage, learner-band, or life-skills values instead.
- Reject any row whose `Display standard code` is duplicated within the same CSV context when that duplication is known programmatically.
- Accept `topic` values joined with ` | ` when one standard genuinely spans multiple topics, the source supports that merged topic cell, and the approved sample contract allows that topic style.
- Accept synthetic `Display standard code` prefixes such as `DA.1.1` when needed to keep the code unique and the approved sample contract allows transformed or prefixed display codes.
- Reject truncated descriptions, cross-row sentence merges, missing required sub-parts, neighboring-row contamination, and forced flattening when the sample style preserves multiline structure.
- Reject symbol loss or incorrect normalization for mathematical notation, chemistry notation, Greek letters, multilingual text, or semantic punctuation.
- Reject noise such as appendix-only out-of-scope items, N.B. notes, repeated headers, repeated footers, page numbers, continuation fragments, layout labels, and extraction artifacts.
- Accept null fields when the value truly is absent.
- Return tag=VALID only when the row is safe to append to the final CSV.
- Return tag=INVALID for every rejection.

Return is_valid=false with explicit issues if anything is unsupported.

Sample CSV transformation contract:
{sample_contract_json}

Extracted row:
{row_json}

Original document markdown:
{parsed_document.markdown}
```

### Programmatic Preflight (non-LLM, before LLM audit)

The critic also runs `_run_contract_preflight` which checks:

- Required columns from sample contract are not blank
- Layout noise / extraction artifacts in field values
- Description truncation relative to citation
- Multiline structure loss
- Special notation / Unicode loss

---

## 8. CSV Audit Mode — Single Row

**Source:** `csv_audit.py` → `_build_audit_messages`  
**When:** `python main.py audit-csv --audit-csv <file>` (legacy single-row path)  
**Model:** Critic model

### System Prompt

```
You are a strict CSV audit agent. Judge extracted rows against their original source and report concrete row-level issues.
```

### User Prompt

```
Audit this extracted CSV row against the original source document and the approved sample contract.

Important audit rules:
- Audit the row directly from the source link content, not from final extraction metadata requirements.
- `Standard code` may be empty and must not be flagged solely for being blank.
- `czi_standard_code` may be empty or absent and must not be flagged solely for being blank or missing.
- `Display standard code` may be synthetic/transformed or source-faithful if that matches the sample contract.
- `topic` may contain multiple source-supported topic names joined with ` | ` when one standard genuinely spans multiple topics and the approved sample contract allows that topic style.
- `Display standard code` must never be duplicated within the same CSV; if duplicates appear, mark every affected row `INVALID`.
- A prefixed display code such as `DA.1.1` or `TOP.1.1` is acceptable when needed to keep the display code unique and the approved sample contract allows that display-code style.
- Detect row-level transformation issues such as wrong subject/domain/topic placement, incorrect display-grade logic, noise, row contamination, truncation, bad merges, and unsupported description wording.
- Do not require `_source_citation` columns in this audit mode.
- Return `VALID` only if the row is materially consistent with the source and sample contract.
- Return `INVALID` with explicit issues when any field looks wrong, unsupported, noisy, or structurally mis-mapped.

Sample CSV transformation contract:
{contract_json}

CSV row to audit:
{row_json}

Original source markdown:
{parsed_document.markdown}
```

---

## 9. CSV Audit Mode — Batch (Production Path)

**Source:** `csv_audit.py` → `_build_batch_audit_messages`  
**When:** Production CSV audit (rows processed in chunks of 12)  
**Model:** Critic model

### System Prompt

```
You are a strict CSV audit agent. Judge extracted rows against their original source and report concrete row-level issues for each row_number.
```

### User Prompt

```
Audit these extracted CSV rows against the original source document and the approved sample contract.

Important audit rules:
- Audit every row directly from the source link content, not from final extraction metadata requirements.
- Return one finding for every input row_number.
- `Standard code` may be empty and must not be flagged solely for being blank.
- `czi_standard_code` may be empty or absent and must not be flagged solely for being blank or missing.
- `Display standard code` may be synthetic/transformed or source-faithful if that matches the sample contract.
- `topic` may contain multiple source-supported topic names joined with ` | ` when one standard genuinely spans multiple topics and the approved sample contract allows that topic style.
- `Display standard code` must never be duplicated within the same CSV; if duplicates appear, mark every affected row `INVALID`.
- A prefixed display code such as `DA.1.1` or `TOP.1.1` is acceptable when needed to keep the display code unique and the approved sample contract allows that display-code style.
- Detect row-level transformation issues such as wrong subject/domain/topic placement, incorrect display-grade logic, noise, row contamination, truncation, bad merges, unsupported description wording, and hierarchy mistakes.
- Do not require `_source_citation` columns in this audit mode.
- Return `VALID` only if the row is materially consistent with the source and sample contract.
- Return `INVALID` with explicit issues when any field looks wrong, unsupported, noisy, or structurally mis-mapped.

Sample CSV transformation contract:
{contract_json}

CSV rows to audit:
{rows_json}

Original source markdown:
{parsed_document.markdown}
```

---

## 10. Sample Contract Rules (Injected into Prompts)

**Source:** `chat_batches.py` → `_derive_sample_contract`  
**Not LLM prompts themselves**, but programmatically inferred from an approved sample CSV and injected as `{sample_contract_json}` into analysis, extraction, review, and critic prompts.

The contract includes these rule categories:

| Field | Purpose |
|---|---|
| `column_order` | Exact CSV column order |
| `required_columns` | Columns that must be populated |
| `subject_naming` | How to name subjects |
| `grade_level_naming` | Broad grade-band style |
| `display_grade_logic` | User-facing grade wording |
| `grade_string_logic` | Grade number/range normalization |
| `display_standard_code_logic` | Source-faithful vs synthetic codes, uniqueness |
| `source_link_format` | Canonical URL vs local file |
| `description_style` | Prose style, capitalization, periods |
| `description_integrity_rules` | No truncation, no cross-row merge |
| `description_multiline_style` | Preserve vs single-cell prose |
| `description_merge_split_style` | Merge sub-parts vs split lines |
| `bullet_and_punctuation_style` | Strip bullets, preserve punctuation |
| `symbol_preservation_rules` | Math, chemistry, Greek, multilingual |
| `field_placement_rules` | Per-column placement (domain, topic, l3–l5, description) |
| `disallowed_output_content` | What must not appear in cells |
| `noise_rejection_rules` | Appendix, N.B., headers, footers |
| `output_quality_rules` | Coverage, uniqueness, consistency |
| `sample_rows` | Exemplar rows from the approved sample |

---

## 11. Codex UI Worker Operational Instructions

**Source:** `codex_worker_instructions.md`  
**Not an LLM prompt**, but governs Codex-chat-assisted batch jobs that reference `sample_csv_column_prompt.md`.

Key job types:

| Action | Goal |
|---|---|
| `draft_sample` | Produce 6–10 real sample rows in `sample_output_template.csv` |
| `run_extraction` | Full extraction using approved sample as contract |
| `audit_batch` | Audit extracted CSV against sources and sample contract |
| `sync_sample` / `sync_final` | Push to Google Sheets when explicitly queued |

Column-meaning inference order for Codex worker:

1. Approved sample CSV rows or sample contract
2. Source document hierarchy and repeated row structure
3. Stable global quality rules from `sample_csv_column_prompt.md`

---

## 12. Retry Loop

When the critic rejects a row, `{prior_error_log}` from the critic is injected into both the **Pre-Extraction Analysis** and **Extraction** prompts as:

```
Previous validation failure log. Correct these exact issues:
{prior_error_log}
```

This repeats up to `max_retries` (configured in `config.py` / `.env`).

---

## Model Routing Summary

| Stage | Default Model Setting | Providers |
|---|---|---|
| Schema inference | `extractor_model` | Gemini (Instructor), Portkey |
| Pre-extraction analysis | `extractor_model` | Gemini (Instructor), Portkey |
| Extraction | `extractor_model` | Gemini (Instructor), Portkey |
| Transformation review | `critic_model` | OpenAI, Anthropic, Portkey |
| Critic audit | `critic_model` | OpenAI, Anthropic, Portkey |
| CSV audit | `critic_model` | OpenAI, Anthropic, Portkey |

---

## Source File Index

| Section | File | Function |
|---|---|---|
| Sample CSV drafting | `sample_csv_column_prompt.md` | — |
| Schema from documents | `chat_batches.py` | `_infer_schema_with_gemini` |
| Schema from instructions | `chat_batches.py` | `_infer_schema_from_instructions_with_gemini` |
| Pre-extraction analysis | `agent_engine.py` | `_build_analysis_messages` |
| Extraction | `agent_engine.py` | `_build_extraction_messages` |
| Transformation review | `validation/reviewer.py` | `_build_review_messages` |
| Critic audit | `validation/critic.py` | `_build_audit_messages` |
| CSV audit (single) | `csv_audit.py` | `_build_audit_messages` |
| CSV audit (batch) | `csv_audit.py` | `_build_batch_audit_messages` |
| Sample contract derivation | `chat_batches.py` | `_derive_sample_contract` |
| Codex worker ops | `codex_worker_instructions.md` | — |
