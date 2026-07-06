---
name: extract-data
description: Triggers the dynamic, layout-aware transformation loop on raw inputs to map into Pydantic CSV models.
---

# Extract Data

Use this skill when the user wants the workspace to transform PDFs, DOCX files, or website references into a verified CSV row set.

Read [AGENTS.md](../../../../AGENTS.md) first, then keep this workflow lean and progressive:

## Operating Mode

This skill should behave like an operating agent, not a one-off script launcher.

- In this workspace, Codex itself is the agent and the chat window is the default control panel.
- Read the user's natural-language message as an operating brief.
- Infer whether the brief is asking for schema drafting, extraction, validation, rerun, or manifest-driven acquisition.
- Infer whether the brief is asking for schema drafting, extraction, validation, rerun, manifest-driven acquisition, or extracted-CSV audit.
- Choose the smallest correct workflow that satisfies the brief while preserving the workspace guardrails.
- Avoid asking the user to translate their request into internal command syntax unless a real ambiguity remains.

## Trigger Signals

- The user shares documents and asks for extraction, structuring, schema drafting, CSV generation, validation, or retry.
- The user uses natural language after uploading files or pasting links in the chat, such as `process these`, `extract these`, `use this schema`, `draft a sample csv`, or equivalent wording.
- The user wants a sample CSV first, or says they do not yet have a sample CSV.
- Files are staged in `./input_documents/` and the next logical action is to inspect or advance the extraction pipeline.

## Default Decision Flow

1. Run `python main.py status` when you need a fast workspace summary.
2. If the current chat request includes a sample CSV, use it as the schema source for that batch.
   - Treat that sample CSV as the transformation contract as well, including column order, naming, formatting, hierarchy placement, and exclusion rules.
   - Treat description fidelity, symbol preservation, multiline handling, and noise rejection as hard contract requirements for the run.
3. If the user previously approved a sample CSV and has not replaced it, keep using that sample CSV as the active contract for later chat turns.
4. If the user provides an extracted CSV, prefer audit mode before any extraction or schema inference.
5. If the user provides source documents or links and there is an approved sample CSV, proceed with extraction using that contract.
6. Otherwise, prefer the approved workspace default schema from `./schema_config.json`.
7. Only if no approved schema is available should you draft schema/template outputs first and stop for human approval.
8. Before extraction, build a source-to-row understanding artifact for the current document:
   - how one complete row is formed
   - what each column contains
   - how each column is derived from the source
   - what source content must be excluded
9. Before finalization, run a transformation evaluation-and-fixing step on the extracted row.
10. Never treat parser success as completion; final completion requires the review/fix step, a `VALID` critic verdict, and a successful CSV append.

## Brief-to-Action Mapping

- If the user says the schema is attached or provides a filled example CSV, treat that CSV as the schema source and transformation contract.
- If the user approves a drafted or uploaded sample CSV, treat that contract as sticky across later chat turns until the user replaces it.
- `Display standard code` may be synthetic/transformed or exactly source-faithful; follow the sample contract for the run instead of assuming it must mirror raw source text.
- If the user uploads a CSV with populated `source` values and asks for extraction, treat it as a manifest unless they clearly say it is schema-only.
- If the user uploads an already extracted CSV and asks to check or audit it, treat it as an extracted-CSV audit input rather than a schema sample or extraction manifest.
- If the user provides only documents or links and asks for extraction, use the workspace default schema.
- If the user provides only instructions and no approved schema, draft a sample CSV first.
- If the user asks to correct prior output, preserve the prior failure context and rerun the affected extraction path instead of starting from scratch.
- If the user provides both a sample CSV and source files or links in the same turn, lock the sample CSV first, then extract against it.

## Execution Workflow

1. Discover raw inputs from `./input_documents/`, from the active batch workspace under `./output/chat_batches/<batch-name>/input_documents/`, or from files/URLs supplied directly in the Codex chat for the current task.
   - If an uploaded CSV is being used as an input manifest and contains a populated `source` column, expand those `source` values into the actual acquisition targets before parsing.
2. Route preprocessing by file type:
   - For website references, run [web_scraper.py](../../../../web_scraper.py), which wraps Crawl4AI-backed parsing from [parsers/web_parser.py](../../../../parsers/web_parser.py).
   - For PDFs, run [pdf_parser.py](../../../../pdf_parser.py), which wraps PyMuPDF-backed parsing from [parsers/pdf_parser.py](../../../../parsers/pdf_parser.py).
   - For DOCX files, use [parsers/docx_parser.py](../../../../parsers/docx_parser.py) through the shared router.
3. Instantiate the analysis-first extraction loop from [agent_engine.py](../../../../agent_engine.py):
   - First build a pre-extraction understanding artifact describing row formation, column derivations, representative row shape, and exclusion rules.
   - Persist that artifact for inspection and reuse it as the plan for extraction.
4. Continue the extraction loop from [agent_engine.py](../../../../agent_engine.py):
   - Create the schema-aware payload model from [schemas.py](../../../../schemas.py).
   - Use Gemini 3.5 Flash via Instructor constrained JSON mode.
   - Require per-document anchoring only after the row-formation understanding step is complete.
5. Run the extracted row through the transformation reviewer in [validation/reviewer.py](../../../../validation/reviewer.py):
   - evaluate whether the transformed row matches the source, sample contract, and row-formation plan
   - apply minimal supported fixes when possible
   - persist a review artifact before final validation
6. Pipe the reviewed JSON to the Critic node in [validation/critic.py](../../../../validation/critic.py):
   - Run the programmatic Pydantic validation gate first.
   - Then run the adversarial semantic audit with the configured high-reasoning critic model.
   - Only continue when the verdict carries a `VALID` tag.
7. If the critic rejects the row, pass the exact error log back into the extractor and retry within the configured retry budget.
8. If retries are exhausted, write the review payload to `manual_review.json` and stop short of any final CSV append.

## CSV Audit Mode

- When the user provides an extracted CSV and asks for validation or issue detection, use `python main.py audit-csv --audit-csv <file>` with an optional `--sample-csv <contract.csv>` override.
- Audit each row against its `source` reference, the active sample contract, the transformation reviewer, and the final critic.
- Produce a structured issue report instead of appending anything to the final CSV.
- If a row fails because the `source` link does not actually point to the content that supports the row, report that as a source-link/support mismatch before suggesting content-level fixes.

## Expected Outputs

- Verified rows append to the active final CSV target.
- Failed rows land in manual review with enough markdown context to debug.
- Sample CSV drafting flows should stop for human approval before full extraction proceeds.
- In normal chat use, the user should be able to upload inputs and describe the task in plain language without restating the workflow.

## Notes

- Prefer `python main.py run-once`, `python main.py watch`, or `python main.py chat-batch ...` when the workspace pipeline already covers the request.
- In chat-first mode, uploaded files should be treated as direct batch inputs and pasted `http(s)` links should be materialized into batch-local references before parsing.
- In this workspace, natural-language extraction requests mean: use the attachments and links from the active user turn as one extraction batch, defaulting to the workspace schema unless the user supplied a new sample CSV.
- A one-row CSV can serve as both schema example and manifest when it includes a populated `source` column; in that case the agent should try to fetch the referenced document or discover a linked PDF from the referenced webpage before extracting.
- Prefer the terminal and status views first; use the local UI flow from [ui_server.py](../../../../ui_server.py) only when the user explicitly wants a browser-based control panel.
