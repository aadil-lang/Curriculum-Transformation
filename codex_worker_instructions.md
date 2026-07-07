# Codex UI Worker

This repository supports a temporary `codex_chat_assisted` execution mode for the local UI.

In that mode:

- the UI remains the intake and review surface
- batch actions enqueue jobs under `output/codex_job_queue/`
- a Codex worker thread claims and processes those jobs
- the worker writes artifacts back into the normal batch folders so the UI can render them

## Generalized Prompt Strategy

Do not hardcode one subject-specific hierarchy pattern across all subjects.

For each job, infer column meaning in this order:

1. approved sample CSV rows or sample contract
2. source document hierarchy and repeated row structure
3. stable global quality rules from `sample_csv_column_prompt.md`

Read `sample_csv_column_prompt.md` before processing jobs that draft samples or extract full CSVs.

Important implications:

- `subject` may be a parent learning area for one subject and a course title for another
- `domain` may be a syllabus title, strand family, framework section, or course grouping
- `topic` may be a single heading or a merged path joined with ` | `
- `grade_level` must be exactly `Elementary School`, `Middle School`, or `High School`
- `display_grade` and `grade_number` may be document-level or row-level depending on the approved sample and source
- `source` may need to be a canonical public URL rather than a local file name when the approved sample expects that pattern

## Queue Commands

List queued jobs:

```bash
./.venv/bin/python main.py codex-job-list
```

Claim the next pending job:

```bash
./.venv/bin/python main.py codex-job-claim-next --worker-id codex_ui_worker
```

Show one job:

```bash
./.venv/bin/python main.py codex-job-show --job-id <job-id>
```

Mark a job running:

```bash
./.venv/bin/python main.py codex-job-update \
  --job-id <job-id> \
  --status running \
  --worker-id codex_ui_worker \
  --message "Processing batch artifacts."
```

Mark a job completed:

```bash
./.venv/bin/python main.py codex-job-update \
  --job-id <job-id> \
  --status completed \
  --worker-id codex_ui_worker \
  --message "Completed Codex-assisted batch job."
```

Mark a job failed:

```bash
./.venv/bin/python main.py codex-job-update \
  --job-id <job-id> \
  --status failed \
  --worker-id codex_ui_worker \
  --message "Reason the job failed."
```

## Batch Locations

Each batch lives under:

```text
output/chat_batches/<batch-name>/
```

Important paths:

- `ui_manifest.json`
- `input_documents/`
- `output/schema_config.json`
- `output/sample_output_template.csv`
- `approved_sample.csv` or another approved sample CSV at batch root
- `output/<subject>.csv` or other final CSV
- `output/codex_job_status.json`

## Job Actions

### `draft_sample`

Goal:

- produce a real draft sample CSV in `output/sample_output_template.csv`
- use actual extracted sample rows when possible
- keep row count aligned with the requested sample size, usually `6-10`

Expectations:

- do not use the local provider-bound extraction pipeline when operating in Codex-chat-assisted mode
- inspect the staged source documents directly
- infer column meanings from the approved sample contract and source rather than from generic cross-subject defaults
- follow the workspace schema/sample contract rules
- write only the user-facing CSV columns into `sample_output_template.csv`
- if extraction cannot be completed safely, leave a truthful minimal artifact rather than fake repeated placeholder rows

### `run_extraction`

Goal:

- use the approved sample CSV as the contract
- produce the batch final CSV in the normal batch output location
- preserve coverage, row order, multiline descriptions, and uniqueness rules from the approved sample

Expectations:

- use Codex reasoning to inspect the source and sample together
- preserve the approved sample's subject-specific column semantics rather than collapsing into generic subject/domain/topic labels
- keep the extracted CSV aligned with the approved sample contract
- write the resulting CSV to the batch `output/` directory

### `audit_batch`

Goal:

- evaluate the extracted CSV against the staged sources and approved sample contract
- persist audit artifacts in the normal batch output area

### `sync_sample` and `sync_final`

Goal:

- sync the corresponding CSV to Google Sheets only when explicitly queued

## Worker Loop

Suggested worker loop:

1. Claim one pending job.
2. Mark it `running`.
3. Read `ui_manifest.json`, source documents, and any approved sample/schema artifacts for the batch.
4. Produce the required batch artifacts.
5. Mark the job `completed` with a concise message.
6. If the job cannot be completed, mark it `failed` with a useful reason.

Process one job per wake-up unless the queue is very small and progress is stable.
