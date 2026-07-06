# Sample CSV Column Prompt

Use this prompt when preparing a sample CSV for curriculum or standards extraction.

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

## Mapping Guidance By Pattern

- If the source has `subject > domain/discipline > strand/topic > coded outcomes`, map those directly to `subject`, `domain`, and `topic`.
- If the source is a single framework document with named domains and named cluster headings, use the domain name in `domain` and the cluster heading text in `topic`.
- If the source is a course with units and outcomes, use the course or framework name in `domain` and the unit heading in `topic`.

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
- Do not move content into the wrong column. Domain, topic, description, and display-code placement must remain aligned with the approved sample.
- Do not truncate, flatten incorrectly, contaminate with neighboring rows, or silently drop meaningful sub-parts from descriptions.
- If a column is not clearly supported by the source and the user has not asked for a derived value, leave it blank.
- If the subject is compiled from multiple links, keep all other column rules the same and only vary `source` row by row as needed.

## Post-Extraction Delivery

- After a CSV is fully extracted, transfer the finalized data to the linked Google Sheet.
- Treat the completed CSV as the canonical extracted file first, then push the same finalized rows into Google Sheets.
- Only transfer to Google Sheets after extraction is complete and ready for delivery.
