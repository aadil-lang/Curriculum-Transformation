from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from config import OUTPUT_DIR, ROOT_DIR


CodexJobAction = Literal[
    "draft_sample",
    "run_extraction",
    "audit_batch",
    "sync_sample",
    "sync_final",
]
CodexJobStatus = Literal[
    "pending",
    "claimed",
    "running",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_JOB_STATUSES = {"completed", "failed", "cancelled"}
ACTIVE_JOB_STATUSES = {"pending", "claimed", "running"}
CODEX_JOB_QUEUE_DIR = OUTPUT_DIR / "codex_job_queue"
CHAT_BATCH_OUTPUT_DIR = ROOT_DIR / "output" / "chat_batches"


@dataclass(slots=True)
class CodexJob:
    id: str
    batch_name: str
    action: CodexJobAction
    payload: dict[str, Any]
    status: CodexJobStatus
    created_at_utc: str
    updated_at_utc: str
    worker_id: str = ""
    message: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


def ensure_codex_job_queue_dir() -> Path:
    CODEX_JOB_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return CODEX_JOB_QUEUE_DIR


def create_codex_job(
    *,
    batch_name: str,
    action: CodexJobAction,
    payload: dict[str, Any] | None = None,
    message: str = "",
) -> CodexJob:
    ensure_codex_job_queue_dir()
    now = _utc_now()
    job = CodexJob(
        id=f"{now.replace(':', '').replace('-', '').replace('.', '')}_{uuid4().hex[:8]}",
        batch_name=batch_name,
        action=action,
        payload=payload or {},
        status="pending",
        created_at_utc=now,
        updated_at_utc=now,
        message=message or f"Queued {action} job for Codex-assisted execution.",
    )
    save_codex_job(job)
    return job


def save_codex_job(job: CodexJob) -> CodexJob:
    ensure_codex_job_queue_dir()
    path = codex_job_path(job.id)
    path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")
    _write_batch_job_status(job)
    return job


def load_codex_job(job_id: str) -> CodexJob:
    payload = json.loads(codex_job_path(job_id).read_text(encoding="utf-8"))
    return CodexJob(**payload)


def codex_job_path(job_id: str) -> Path:
    return ensure_codex_job_queue_dir() / f"{job_id}.json"


def list_codex_jobs(
    *,
    batch_name: str | None = None,
    statuses: set[str] | None = None,
    limit: int | None = None,
) -> list[CodexJob]:
    ensure_codex_job_queue_dir()
    jobs: list[CodexJob] = []
    for path in sorted(CODEX_JOB_QUEUE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        job = CodexJob(**payload)
        if batch_name and job.batch_name != batch_name:
            continue
        if statuses and job.status not in statuses:
            continue
        jobs.append(job)
    jobs.sort(key=lambda item: (item.created_at_utc, item.id))
    if limit is not None:
        return jobs[:limit]
    return jobs


def latest_codex_job_for_batch(batch_name: str) -> CodexJob | None:
    jobs = list_codex_jobs(batch_name=batch_name)
    if not jobs:
        return None
    jobs.sort(key=lambda item: (item.updated_at_utc, item.created_at_utc, item.id), reverse=True)
    return jobs[0]


def active_codex_job_for_batch(batch_name: str) -> CodexJob | None:
    jobs = list_codex_jobs(batch_name=batch_name, statuses=ACTIVE_JOB_STATUSES)
    if not jobs:
        return None
    jobs.sort(key=lambda item: (item.created_at_utc, item.id))
    return jobs[0]


def codex_job_snapshot_for_batch(batch_name: str) -> dict[str, Any]:
    active_job = active_codex_job_for_batch(batch_name)
    latest_job = latest_codex_job_for_batch(batch_name)
    job = active_job or latest_job
    if job is None:
        return {}
    return asdict(job)


def claim_next_codex_job(worker_id: str) -> CodexJob | None:
    pending_jobs = list_codex_jobs(statuses={"pending"})
    if not pending_jobs:
        return None
    job = pending_jobs[0]
    return update_codex_job(
        job.id,
        status="claimed",
        worker_id=worker_id,
        message=f"Claimed by {worker_id}.",
    )


def update_codex_job(
    job_id: str,
    *,
    status: CodexJobStatus | None = None,
    worker_id: str | None = None,
    message: str | None = None,
    result: dict[str, Any] | None = None,
) -> CodexJob:
    job = load_codex_job(job_id)
    if status is not None:
        job.status = status
    if worker_id is not None:
        job.worker_id = worker_id
    if message is not None:
        job.message = message
    if result is not None:
        job.result = result
    job.updated_at_utc = _utc_now()
    return save_codex_job(job)


def codex_job_status_path(batch_name: str) -> Path:
    return CHAT_BATCH_OUTPUT_DIR / batch_name / "output" / "codex_job_status.json"


def _write_batch_job_status(job: CodexJob) -> None:
    path = codex_job_status_path(job.batch_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
