# Sample CSV Column Prompt

Use this prompt when preparing a sample CSV for curriculum or standards extraction.

## Generalization Principle

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

## Reusable Prompt

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

## Row Formation Rules

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

## Column-Semantics Inference Rules

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

## Mapping Guidance By Pattern

- If the source has `subject > domain/discipline > strand/topic > coded outcomes`, map those directly to `subject`, `domain`, and `topic`.
- If the source is a single framework document with named domains and named cluster headings, use the domain name in `domain` and the cluster heading text in `topic`.
- If the source is a course with units and outcomes, use the course or framework name in `domain` and the unit heading in `topic`.

## Prompt Workflow

For every subject, perform the prompt reasoning in this order:

1. Read the approved sample contract or approved sample rows and infer what each output column means for this subject.
2. Read the source and identify the repeated row unit, hierarchy labels, and noise to exclude.
3. Write a source-to-row mapping that explains how one complete row is formed.
4. Draft or extract rows only after that mapping is established.
5. Review whether the resulting rows still match the sample-derived column semantics.

## Output Quality Rules

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

## Post-Extraction Delivery

- After a CSV is fully extracted, transfer the finalized data to the linked Google Sheet.
- Treat the completed CSV as the canonical extracted file first, then push the same finalized rows into Google Sheets.
- Only transfer to Google Sheets after extraction is complete and ready for delivery.
