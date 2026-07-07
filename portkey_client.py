from __future__ import annotations

import logging
import threading
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

LOGGER = logging.getLogger(__name__)

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


def _is_infra_error(exc: Exception) -> bool:
    """True for transient provider/gateway failures worth failing over on.

    5xx, timeouts, and connection errors are provider-side; validation errors
    (bad schema) are NOT — failing those over to another provider won't help.
    """
    name = type(exc).__name__
    if name in {"APITimeoutError", "APIConnectionError", "InternalServerError", "APIStatusError"}:
        return True
    text = f"{name} {exc}".lower()
    return any(
        marker in text
        for marker in ("internal server error", "timeout", "timed out", "502", "503", "504", "520", "connection")
    )


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
    fallbacks: list[tuple[str, str]] | None = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> T:
    """Call a Portkey-routed model and return a schema-validated Pydantic object.

    Uses Instructor over an OpenAI-compatible client pointed at the Portkey
    gateway so the provider response is coerced to `response_model` and
    automatically re-prompted on validation failure.

    `fallbacks` is an ordered list of (provider, model) pairs tried in turn when
    the primary target fails with a transient infra error (5xx/timeout). Each
    target needs its own model id because the same Claude model is named
    differently per cloud (Vertex: `anthropic.claude-opus-4-6`; Azure Sweden:
    `claude-opus-4-6`). Validation errors are NOT failed over — they re-raise.
    """
    targets: list[tuple[str, str]] = [(provider, model), *(fallbacks or [])]
    last_exc: Exception | None = None
    for index, (target_provider, target_model) in enumerate(targets):
        try:
            return _call_one_target(
                api_key=api_key,
                provider=target_provider,
                model=target_model,
                response_model=response_model,
                messages=messages,
                max_retries=max_retries,
                max_tokens=max_tokens,
                max_concurrency=max_concurrency,
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - decide below whether to fail over
            last_exc = exc
            is_last = index == len(targets) - 1
            if is_last or not _is_infra_error(exc):
                raise
            LOGGER.warning(
                "Provider %s (%s) failed with %s; failing over to %s (%s).",
                target_provider,
                target_model,
                type(exc).__name__,
                targets[index + 1][0],
                targets[index + 1][1],
            )
    assert last_exc is not None  # unreachable: loop either returns or raises
    raise last_exc


def _call_one_target(
    *,
    api_key: str,
    provider: str,
    model: str,
    response_model: type[T],
    messages: list[dict[str, str]],
    max_retries: int,
    max_tokens: int,
    max_concurrency: int,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> T:
    import instructor
    from openai import OpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    openai_client = OpenAI(
        api_key="portkey",  # actual credentials travel in the Portkey headers
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=createHeaders(api_key=api_key, provider=provider),
        timeout=timeout,
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
