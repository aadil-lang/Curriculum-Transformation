from __future__ import annotations

import threading
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TOKENS = 16000
DEFAULT_MAX_CONCURRENCY = 12
# Hard per-request timeout. Without it, a stalled socket after a successful HTTP
# response hangs the worker thread forever, holds a concurrency slot, and deadlocks
# the whole run (observed on large extractions). A timeout turns a stall into a
# retryable error instead.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0

# A process-wide cap on in-flight LLM calls. With per-source and per-row
# parallelism stacked, unbounded concurrency triggers provider 5xx/rate limits
# (we observed Vertex 520s under load), so every structured call passes through
# this semaphore. Sized once on first use.
_semaphore_lock = threading.Lock()
_semaphore: threading.Semaphore | None = None
_semaphore_limit: int | None = None


def _get_semaphore(limit: int) -> threading.Semaphore:
    global _semaphore, _semaphore_limit
    with _semaphore_lock:
        if _semaphore is None:
            _semaphore = threading.Semaphore(limit)
            _semaphore_limit = limit
        return _semaphore


def call_portkey_structured(
    *,
    api_key: str,
    provider: str,
    model: str,
    response_model: type[T],
    messages: list[dict[str, str]],
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY,
) -> T:
    """Call a Portkey-routed model and return a schema-validated Pydantic object.

    Uses Instructor over an OpenAI-compatible client pointed at the Portkey
    gateway so the provider response is coerced to `response_model` and
    automatically re-prompted on validation failure. This replaces raw
    json_object parsing, which was fragile to key-casing drift and missing
    required fields.
    """
    import instructor
    from openai import OpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    openai_client = OpenAI(
        api_key="portkey",  # actual credentials travel in the Portkey headers
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=createHeaders(api_key=api_key, provider=provider),
        timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS,
        max_retries=2,
    )
    # MD_JSON tolerates fenced ```json blocks, which Claude via Vertex emits.
    client = instructor.from_openai(openai_client, mode=instructor.Mode.MD_JSON)
    with _get_semaphore(max_concurrency):
        return client.chat.completions.create(
            model=model,
            response_model=response_model,
            messages=messages,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )
