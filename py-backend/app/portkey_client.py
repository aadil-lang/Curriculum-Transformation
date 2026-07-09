from __future__ import annotations

import concurrent.futures
import logging
import random
import threading
import time
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 2
DEFAULT_MAX_TOKENS = 16000
DEFAULT_MAX_CONCURRENCY = 12
# Failover behavior for transient provider/gateway 5xx: cycle the whole target
# chain up to this many rounds, backing off between attempts so a short outage
# window is ridden out instead of exhausting all providers in <1s. Backoff grows
# per attempt (capped) with jitter to avoid thundering-herd retries.
_FAILOVER_ROUNDS = 3
_FAILOVER_BASE_BACKOFF = 2.0
_FAILOVER_MAX_BACKOFF = 30.0
# Hard per-request timeout. Without it, a stalled socket after a successful HTTP
# response hangs the worker thread forever, holds a concurrency slot, and deadlocks
# the whole run (observed on large extractions). A timeout turns a stall into a
# retryable error instead.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 180.0
# Wall-clock backstop multiplier: the watchdog abandons a call after
# timeout * this factor even if the socket-level timeout never fires (observed:
# a gateway that accepts the request then goes silent can defeat httpx timeouts
# and hang for hours). This is the guaranteed ceiling per call.
_WATCHDOG_MARGIN = 1.5
_WATCHDOG_MIN_SECONDS = 60.0
# Shared pool for watchdog-wrapped calls. Daemon threads so an abandoned (truly
# wedged) call can never block interpreter exit.
_watchdog_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=64, thread_name_prefix="llm-watchdog"
)

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
    if name in {"APITimeoutError", "APIConnectionError", "InternalServerError", "APIStatusError", "TimeoutError"}:
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
    differently per cloud (Vertex: `anthropic.claude-3-5-sonnet@20240620`; Azure Sweden:
    `claude-3-5-sonnet-20240620`). Validation errors are NOT failed over — they re-raise.
    """
    targets: list[tuple[str, str]] = [(provider, model), *(fallbacks or [])]
    # Cycle the chain over several rounds with growing backoff. A transient 5xx
    # window is common (observed 502/520/500 under load); instantly exhausting
    # every provider in under a second just guarantees failure, whereas a short
    # backoff lets the provider recover. Validation errors short-circuit (raise
    # immediately) since another provider or wait won't help.
    attempts = [(rnd, tp, tm) for rnd in range(_FAILOVER_ROUNDS) for (tp, tm) in targets]
    last_exc: Exception | None = None
    for attempt_index, (round_number, target_provider, target_model) in enumerate(attempts):
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
            is_last = attempt_index == len(attempts) - 1
            if is_last or not _is_infra_error(exc):
                raise
            # Backoff grows with the round so the first failover is quick (try the
            # other cloud immediately) but repeated failures wait progressively.
            backoff = min(_FAILOVER_MAX_BACKOFF, _FAILOVER_BASE_BACKOFF * (2 ** round_number))
            backoff *= 0.5 + random.random()  # jitter in [0.5x, 1.5x]
            next_provider, next_model = attempts[attempt_index + 1][1], attempts[attempt_index + 1][2]
            LOGGER.warning(
                "Provider %s (%s) failed with %s; backing off %.1fs then trying %s (%s).",
                target_provider,
                target_model,
                type(exc).__name__,
                backoff,
                next_provider,
                next_model,
            )
            time.sleep(backoff)
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
    import httpx
    import instructor
    from openai import OpenAI
    from portkey_ai import PORTKEY_GATEWAY_URL, createHeaders

    # Explicit per-phase timeout. A bare float can fail to bound the read phase
    # when a gateway accepts the request then dribbles/stalls the body; naming
    # read=timeout makes a silent server a bounded, retryable error.
    http_timeout = httpx.Timeout(timeout, connect=30.0, read=timeout, write=60.0, pool=30.0)
    openai_client = OpenAI(
        api_key="portkey",  # actual credentials travel in the Portkey headers
        base_url=PORTKEY_GATEWAY_URL,
        default_headers=createHeaders(api_key=api_key, provider=provider),
        timeout=http_timeout,
        max_retries=2,
    )
    # MD_JSON tolerates fenced ```json blocks, which Claude via Vertex emits.
    client = instructor.from_openai(openai_client, mode=instructor.Mode.MD_JSON)

    def _do_call() -> T:
        return client.chat.completions.create(
            model=model,
            response_model=response_model,
            messages=messages,
            max_retries=max_retries,
            max_tokens=max_tokens,
        )

    # Wall-clock watchdog: the socket timeout should fire first, but if a truly
    # wedged call defeats it, this abandons the call at a hard ceiling so the
    # worker thread and its concurrency slot are freed and failover can proceed.
    # The client's own max_retries can stack multiple socket-timeout windows, so
    # the ceiling budgets for that plus a margin.
    hard_ceiling = max(_WATCHDOG_MIN_SECONDS, timeout * (max_retries + 1) * _WATCHDOG_MARGIN)
    with _get_semaphore(max_concurrency):
        future = _watchdog_pool.submit(_do_call)
        try:
            return future.result(timeout=hard_ceiling)
        except concurrent.futures.TimeoutError as exc:
            # The abandoned future keeps running on its daemon thread but its
            # result is discarded; surface a retryable timeout to the caller.
            future.cancel()
            raise TimeoutError(
                f"LLM call exceeded hard wall-clock ceiling of {hard_ceiling:.0f}s "
                f"(provider={provider}, model={model})."
            ) from exc
