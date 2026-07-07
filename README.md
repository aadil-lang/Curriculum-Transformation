# Curriculum-Transformation

Document-to-CSV extraction agent for curriculum and standards sources. The agent turns PDFs, DOCX files, webpage URLs, and source manifests into validated CSV output using an approved sample CSV as the transformation contract.

## What This Agent Does

- drafts sample CSVs from plain-English instructions when no approved sample exists
- derives a schema and transformation contract from an approved sample CSV
- extracts rows from PDFs, DOCX files, webpages, and source-link manifests
- preserves hierarchy, multiline descriptions, symbols, and multilingual text
- validates rows before append using review and critic stages
- audits extracted CSVs against their sources
- syncs approved sample CSVs or final extracted CSVs to Google Sheets only when explicitly requested

## Core Workflow

The agent is designed to follow this sequence:

1. Prepare or approve a sample CSV.
2. Use that approved sample as the schema and style contract.
3. Run full extraction.
4. Audit the extracted CSV.
5. Optionally sync to Google Sheets with a manual command.

## Repository Layout

- `main.py`: CLI entrypoint
- `chat_batches.py`: sample/schema drafting and batch orchestration
- `pipeline.py`: extraction, review, critic validation, and final CSV append
- `csv_audit.py`: extracted CSV auditing
- `google_sheets_sync.py`: Google Sheets delivery
- `schema_config.json`: default workspace schema
- `sample_csv_column_prompt.md`: human-readable column and quality rules
- `input_documents/`: staged raw inputs
- `output/`: runtime artifacts and extracted outputs

## Setup

Requirements:

- Python 3.12+
- a virtual environment
- provider credentials for extraction and critic calls
- **System tools (not pip-installable):**
  - **LibreOffice** — converts legacy `.doc` syllabuses to `.docx` for parsing (`soffice --headless`). Install with `brew install --cask libreoffice` (macOS) or your package manager. Only needed if you extract from `.doc` files.
  - **Playwright browsers** — used by Crawl4AI to scrape webpage sources. Installed via `crawl4ai-setup` (below).

Suggested setup:

```bash
git clone https://github.com/aadil-lang/Curriculum-Transformation.git
cd Curriculum-Transformation
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
crawl4ai-setup                 # installs the Playwright browsers Crawl4AI needs
cp .env.example .env
python main.py bootstrap
python main.py verify
```

For `.doc` support, also install LibreOffice (see System tools above).

## Environment Variables

Minimal `.env` values are documented in `.env.example`.

Common fields:

- `PORTKEY_API_KEY`
- `PORTKEY_EXTRACTOR_PROVIDER`
- `PORTKEY_CRITIC_PROVIDER`
- `GEMINI_API_KEY`
- `OPENAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `EXTRACTOR_MODEL`
- `CRITIC_MODEL`
- `EXECUTION_MODE`
- `SCHEMA_CONFIG_PATH`

Supported execution modes:

- `direct_provider`
- `codex_chat_assisted`

For local standalone CLI runs, you do not have to use `PORTKEY_API_KEY`.

Direct local-provider mode is supported by leaving `PORTKEY_API_KEY` blank and setting provider keys directly instead.

Current practical setup options are:

- extractor through `GEMINI_API_KEY`
- critic/review through `OPENAI_API_KEY`
- critic/review through `ANTHROPIC_API_KEY`

If you want to run the critic side on OpenAI or Codex-family models locally, configure `OPENAI_API_KEY`, set `CRITIC_PROVIDER=openai`, and choose the model in `CRITIC_MODEL`. In other words, Portkey is optional for local use; it is a gateway option, not a requirement.

Example local `.env` direction without Portkey:

```env
PORTKEY_API_KEY=
GEMINI_API_KEY=your_gemini_key
OPENAI_API_KEY=your_openai_key
CRITIC_PROVIDER=openai
CRITIC_MODEL=gpt-5.5
EXECUTION_MODE=direct_provider
```

## Execution Modes

The repository now exposes a temporary execution-mode switch so the UI can stay stable while the execution surface changes.

`direct_provider`

- current default mode
- the local Python backend makes extraction, review, and critic model calls directly through configured providers
- suitable for standalone CLI or UI operation when provider credentials are configured

`codex_chat_assisted`

- temporary transition mode
- intended for periods where Codex chat is the practical execution surface, while the UI remains the intake, review, and artifact surface
- useful when you want to preserve the current UI shape and later return to direct-provider execution cleanly

Important current limitation:

- setting `EXECUTION_MODE=codex_chat_assisted` does not magically make the local Python UI backend call Codex app models by itself
- it is a configuration contract and operating-mode signal for the workspace, status outputs, and future queue-worker integration
- the visible UI can remain the same while you temporarily use Codex chat as the real execution surface

### Using The Agent Through Codex Chat

If you are using this repository inside Codex chat as a chat-based agent workflow, you may not need to configure `PORTKEY_API_KEY` yourself.

In that mode:

- Codex chat is the control surface
- you can give natural-language extraction requests directly in chat
- you can upload files or paste URLs in chat
- manual Portkey configuration is not required just to operate the workflow through Codex chat

This is different from running the standalone CLI yourself with `python main.py ...`.

Short version:

- Codex chat-based usage: Portkey key is not required from you
- standalone CLI usage: Portkey is optional, but some provider configuration is still required with the current implementation
- temporary `codex_chat_assisted` mode: preserves the current UI shape while treating Codex chat as the execution surface for the time being

Google Sheets related configuration is optional and only needed if you plan to use manual sheet sync.

## Main Commands

Check workspace status:

```bash
python main.py status
```

Bootstrap directories:

```bash
python main.py bootstrap
```

Verify environment:

```bash
python main.py verify
```

Run pending staged documents once:

```bash
python main.py run-once
```

Watch `input_documents/` continuously:

```bash
python main.py watch
```

Start the local UI:

```bash
python main.py ui
```

## Sample CSV and Schema Workflow

If no approved sample exists yet, draft one from instructions:

```bash
python main.py chat-batch \
  --name child_studies_sample \
  --instructions "Prepare a sample CSV for Child Studies 7-10. Use 6 to 10 rows." \
  --infer-schema
```

If you want only the schema/template artifacts without extraction:

```bash
python main.py chat-batch \
  --name child_studies_sample \
  --instructions "Prepare a sample CSV for Child Studies 7-10. Use 6 to 10 rows." \
  --infer-schema \
  --draft-only
```

If you have an approved sample CSV and want to make it the workspace default:

```bash
python main.py set-default-schema --sample-csv /absolute/path/to/approved-sample.csv
```

## Full Extraction

Run a single batch using an approved sample CSV and one or more files or URLs:

```bash
python main.py chat-batch \
  --name child_studies_full \
  --sample-csv /absolute/path/to/approved-sample.csv \
  https://curriculum.nsw.edu.au/learning-areas/pdhpe/child-studies-7-10-2025/outcomes
```

Use a custom output CSV filename if needed:

```bash
python main.py chat-batch \
  --name dance_full \
  --sample-csv /absolute/path/to/dance-S.csv \
  --output-csv-name dance.csv \
  https://www.edu.gov.mb.ca/k12/framework/english/arts/dance/docs/dance_gr9_en.pdf
```

Run multiple subjects in one config file:

```bash
python main.py chat-batch --config /absolute/path/to/chat_batch_request.json
```

## CSV Audit

Audit a full extracted CSV against the active workspace contract:

```bash
python main.py audit-csv --audit-csv /absolute/path/to/extracted.csv
```

Audit against a specific approved sample CSV instead:

```bash
python main.py audit-csv \
  --audit-csv /absolute/path/to/extracted.csv \
  --sample-csv /absolute/path/to/approved-sample.csv
```

## Google Sheets Sync

Google Sheets sync is manual by design. It does not run automatically during ordinary extraction.

Authorize OAuth user login:

```bash
python main.py google-oauth-login --client-secret /absolute/path/to/client_secret.json
```

Sync a final extracted CSV:

```bash
python main.py sync-sheet --csv /absolute/path/to/extracted.csv
```

Sync an approved sample CSV without full extracted-CSV audit:

```bash
python main.py sync-sheet --csv /absolute/path/to/sample.csv --sample
```

## Extraction Rules the Agent Follows

The active approved sample CSV is the contract for:

- column order
- naming and formatting
- which fields stay blank by default
- `Display standard code` style
- whether merged topic cells are allowed
- whether prefixed or formed display codes are allowed
- description multiline and merge behavior

Important standing rules captured in the prompt and inferred contract include:

- `Standard code` stays blank unless explicitly requested
- `l3`, `l4`, and `l5` stay blank unless the approved sample supports them
- `Display standard code` must be unique across the CSV
- duplicate display codes may only be disambiguated in a way supported by the approved sample
- `topic` may use ` | ` only when the approved sample supports merged topic cells
- numeric grade ranges like `9-12` become `9,10,11,12`
- no spaces after commas in expanded grade-number lists
- multiline descriptions must not be truncated
- math symbols, notation, and multilingual text must be preserved faithfully
- coverage must be complete for all valid rows supported by the source and sample contract

## Output and Runtime Artifacts

Common runtime locations:

- `output/final_extracted_data.csv`
- `output/manual_review.json`
- `output/csv_audits/`
- `output/analysis_plans/`
- `output/transformation_reviews/`
- `output/chat_batches/<batch-name>/`

The repository ignores local runtime artifacts, staged input documents, OAuth tokens, and output files so the codebase stays shareable.

## Sharing the Agent

Other users can clone the repository and run the agent, but they still need to provide:

- their own `.env`
- their own model credentials
- their own Google OAuth setup if they want Sheets sync
- their own approved sample CSVs or default schema

## Troubleshooting

If `verify` fails:

- confirm the virtual environment is active
- install dependencies from `requirements.txt`
- check `.env` values

If extraction is rejected:

- inspect `output/manual_review.json`
- inspect `output/transformation_reviews/`
- run `python main.py audit-csv --audit-csv <csv>`

If Google Sheets sync fails:

- confirm OAuth login completed
- confirm the linked spreadsheet is accessible to the authenticated account
- confirm the spreadsheet ID and related settings are configured
