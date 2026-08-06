from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
from pathlib import Path
import random
import re
import time
from typing import Any, AsyncIterator
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .codebuff import (
    CodebuffAccountLease,
    CodebuffAccountPool,
    CodebuffClient,
    CodebuffError,
    FreebuffRun,
    SessionManager,
    utc_now_iso,
)
from .config import Settings, load_settings
from .logging_config import configure_logging, redact_headers, render_debug, request_id_var
from .metrics import Metrics
from .openai_compat import (
    CompletionAccumulator,
    build_upstream_payload,
    normalize_chat_messages,
    sanitize_stream_chunk,
)
from .models import CONTEXT_PRUNER_AGENT_ID, FreebuffModel, models_response, resolve_model
from .sse import decode_sse_data, encode_sse


logger = logging.getLogger("freebuff2api.app")

MAX_CHAT_RETRIES = 2

_RATE_LIMIT_MAX_DELAY_MS = 5000
_RATE_LIMIT_DEFAULT_DELAY_MS = 500


def _rate_limit_delay_ms(error: CodebuffError, jitter_ms: int) -> float:
    """Compute the sleep before retrying a rate-limited request (R5).

    Honors upstream hints in priority order:
      1. retry_after_ms (explicit, when present and > 0)
      2. reset_at (ISO-8601) — wait until the window actually resets
      3. 500 ms default
    Caps at 5 s, then adds uniform jitter in [0, jitter_ms] unless jitter_ms
    is <= 0 (env FREEBUFF_RETRY_JITTER=0 disables jitter entirely).
    """
    delay_ms: float = _RATE_LIMIT_DEFAULT_DELAY_MS
    if error.retry_after_ms and error.retry_after_ms > 0:
        delay_ms = float(error.retry_after_ms)
    elif error.reset_at:
        try:
            reset_dt = datetime.fromisoformat(error.reset_at.replace("Z", "+00:00"))
            remaining_ms = (reset_dt - datetime.now(timezone.utc)).total_seconds() * 1000.0
            if remaining_ms > 0:
                delay_ms = remaining_ms
        except (ValueError, TypeError):
            pass  # malformed reset_at -> fall through to default
    delay_ms = min(delay_ms, _RATE_LIMIT_MAX_DELAY_MS)
    if jitter_ms > 0:
        delay_ms += random.uniform(0, float(jitter_ms))
    return delay_ms

_METRIC_PATH_RE = re.compile(r"/[0-9a-f]{8,}\b|/\d+\b")

# Routes this app serves. Anything else (404s, scanner probes) collapses to a
# single "{other}" label so Prometheus label cardinality stays bounded.
_KNOWN_METRIC_PATHS = frozenset({
    "/healthz",
    "/livez",
    "/readyz",
    "/metrics",
    "/v1/models",
    "/v1/chat/completions",
})


def _metric_path(path: str) -> str:
    """Collapse dynamic segments and unknown routes so label cardinality stays bounded."""
    if path in _KNOWN_METRIC_PATHS:
        return path
    normalized = _METRIC_PATH_RE.sub("/{id}", path)
    if normalized in _KNOWN_METRIC_PATHS:
        return normalized
    return "{other}"


def _rss_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = load_settings()
    configure_logging(settings)
    accounts = CodebuffAccountPool(settings)
    app.state.settings = settings
    app.state.accounts = accounts
    app.state.codebuff = accounts.default_client
    app.state.sessions = accounts.default_sessions
    app.state.metrics = Metrics()
    logger.info("configured freebuff accounts count=%s", accounts.account_count)
    try:
        yield
    finally:
        await accounts.aclose()


app = FastAPI(title="freebuff2api", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def _observability_middleware(request: Request, call_next: Any) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    request_id_var.set(request_id)
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start
        request.app.state.metrics.observe(
            request.method,
            _metric_path(request.url.path),
            500,
            duration,
        )
        raise
    response.headers.setdefault("X-Request-ID", request_id)

    if isinstance(response, StreamingResponse):
        # Measure full SSE duration, not just time-to-first-chunk. Wrap the
        # body iterator so the histogram reflects the real stream length.
        original_iterator = response.body_iterator

        async def _timed_stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                duration = time.perf_counter() - start
                request.app.state.metrics.observe(
                    request.method,
                    _metric_path(request.url.path),
                    response.status_code,
                    duration,
                )
                logger.info(
                    "http request method=%s path=%s status=%s duration_ms=%.1f stream=1",
                    request.method,
                    request.url.path,
                    response.status_code,
                    duration * 1000.0,
                )

        response.body_iterator = _timed_stream()
        return response

    duration = time.perf_counter() - start
    request.app.state.metrics.observe(
        request.method,
        _metric_path(request.url.path),
        response.status_code,
        duration,
    )
    logger.info(
        "http request method=%s path=%s status=%s duration_ms=%.1f stream=0",
        request.method,
        request.url.path,
        response.status_code,
        duration * 1000.0,
    )
    return response


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _client(request: Request) -> CodebuffClient:
    return request.app.state.codebuff


def _sessions(request: Request) -> SessionManager:
    return request.app.state.sessions


def _accounts(request: Request) -> CodebuffAccountPool:
    return request.app.state.accounts


def _check_local_auth(request: Request) -> None:
    settings = _settings(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header:
        raise HTTPException(status_code=401, detail="Missing API key")
    
    # backward-compatible single key from env
    if settings.local_api_key:
        expected = f"Bearer {settings.local_api_key}"
        if auth_header == expected:
            return
    
    # multi-key from api_keys.json
    import json, os
    data_dir = Path(os.getenv("FREEBUFF2API_DATA_DIR", os.path.expanduser("~/.freebuff2api")))
    keys_file = data_dir / "api_keys.json"
    if keys_file.exists():
        try:
            with open(keys_file, "r", encoding="utf-8") as f:
                keys = json.load(f)
            for k in keys:
                if k.get("enabled", True) and auth_header == f"Bearer {k.get('key', '')}":
                    return
        except Exception:
            pass
    
    raise HTTPException(status_code=401, detail="Invalid API key")


def _error_response(error: Exception) -> JSONResponse:
    if isinstance(error, CodebuffError):
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            },
        )
    raise error


@app.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    _check_local_auth(request)
    return {"status": "ok"}


@app.get("/livez")
async def livez() -> dict[str, Any]:
    """Unauthenticated liveness probe — the process is up and serving."""
    return {"status": "alive"}


@app.get("/readyz")
async def readyz(request: Request) -> JSONResponse:
    """Readiness probe: 200 iff a token is configured and at least one healthy
    upstream account exists. 503 otherwise (no token, or all quarantined)."""
    accounts = _accounts(request)
    healthy = accounts.healthy_account_count()
    total = accounts.account_count
    token_configured = bool(_settings(request).codebuff_tokens)
    ready = token_configured and healthy >= 1
    return JSONResponse(
        status_code=200 if ready else 503,
        content={
            "status": "ready" if ready else "not_ready",
            "accounts_total": total,
            "accounts_healthy": healthy,
            "token_configured": token_configured,
        },
    )


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus text-format metrics (exposition format 0.0.4)."""
    accounts = _accounts(request)
    body = request.app.state.metrics.render(
        accounts_total=accounts.account_count,
        accounts_healthy=accounts.healthy_account_count(),
        accounts_busy=accounts.busy_account_count(),
        rss_bytes=_rss_bytes(),
    )
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    _check_local_auth(request)
    return models_response()


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Any:
    _check_local_auth(request)
    body = await request.json()
    settings = _settings(request)
    try:
        model_config = resolve_model(body.get("model"))
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    model = model_config.id
    logger.info(
        "chat completion request model=%s stream=%s messages=%s",
        model,
        body.get("stream") is True,
        len(body.get("messages") or []),
    )
    if settings.debug:
        logger.debug(
            "incoming request headers=%s",
            redact_headers(dict(request.headers)),
        )
        logger.debug(
            "chat completion request body=%s",
            render_debug(body, settings.log_body_chars),
        )

    messages = normalize_chat_messages(
        body.get("messages"),
        cli_path=settings.cli_path,
    )
    last_error: CodebuffError | None = None

    for attempt in range(MAX_CHAT_RETRIES + 1):
        lease: CodebuffAccountLease | None = None
        try:
            lease = await _accounts(request).acquire_session(
                model_config.session_id,
                messages=messages,
            )
            client = lease.client
            await client.request_ad_chain(messages=messages)
            await client.validate_agents()
            run = await _start_freebuff_run_chain(client, model_config)
            trace_session_id = str(uuid.uuid4())
            payload = build_upstream_payload(
                {**body, "messages": messages},
                session=lease.session,
                run_id=run.payload_run_id,
                client_id=settings.client_id,
                trace_session_id=trace_session_id,
                upstream_model_id=model_config.upstream_id,
                cli_path=settings.cli_path,
            )
            if settings.debug:
                logger.debug(
                    "prepared upstream chat trace=%s run=%s payload=%s",
                    trace_session_id,
                    run,
                    render_debug(payload, settings.log_body_chars),
                )

            if body.get("stream") is True:
                return StreamingResponse(
                    _stream_openai_chunks(request, payload, run, model, account_lease=lease),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            response = await _collect_completion(
                request,
                payload,
                run,
                model,
                client=lease.client,
            )
            await lease.aclose()
            return JSONResponse(response)

        except CodebuffError as error:
            if lease is not None:
                await lease.aclose()
            if error.is_session_error:
                logger.warning(
                    "session error on attempt %s/%s: %s",
                    attempt + 1,
                    MAX_CHAT_RETRIES + 1,
                    error,
                )
                if lease is not None:
                    try:
                        await lease.sessions.invalidate_session(
                            model_config.session_id,
                            reason="session_error_retry",
                        )
                    except Exception:
                        pass
                last_error = error
                continue
            if error.is_rate_limit and attempt < MAX_CHAT_RETRIES:
                if lease is not None:
                    lease.mark_rate_limited(model, error.reset_at)
                delay_ms = _rate_limit_delay_ms(error, settings.retry_jitter_ms)
                logger.warning(
                    "rate limit on attempt %s/%s, retrying in %.0f ms: %s",
                    attempt + 1,
                    MAX_CHAT_RETRIES,
                    delay_ms,
                    error,
                )
                await asyncio.sleep(delay_ms / 1000.0)
            last_error = error
            if error.is_rate_limit and attempt < MAX_CHAT_RETRIES:
                continue
            return _error_response(error)

        except Exception as error:
            if lease is not None:
                await lease.aclose()
            logger.exception("failed to prepare chat completion")
            return _error_response(error)

    # all retries exhausted
    return _error_response(last_error) if last_error is not None else JSONResponse(
        status_code=502,
        content={"error": {"message": "all accounts exhausted", "type": "upstream_error", "code": "exhausted"}},
    )


async def _stream_openai_chunks(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    account_lease: CodebuffAccountLease | None = None,
    client: CodebuffClient | None = None,
) -> AsyncIterator[bytes]:
    message_id: str | None = None
    client = client or (account_lease.client if account_lease else _client(request))
    settings = _settings(request)
    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                if settings.debug:
                    logger.debug(
                        "chat stream done run_id=%s message_id=%s",
                        run.run_id,
                        message_id,
                    )
                yield encode_sse("[DONE]")
                break

            message_id = data.get("id") or message_id
            chunk = sanitize_stream_chunk(data)
            if chunk is not None:
                if settings.debug:
                    logger.debug(
                        "chat stream chunk=%s",
                        render_debug(chunk, settings.log_body_chars),
                    )
                yield encode_sse(chunk)
            elif settings.debug:
                logger.debug(
                    "chat stream ignored data=%s",
                    render_debug(data, settings.log_body_chars),
                )
    except CodebuffError as error:
        if error.is_rate_limit and account_lease is not None:
            account_lease.mark_rate_limited(model, error.reset_at)
        logger.warning(
            "chat stream failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=settings.debug,
        )
        yield encode_sse(
            {
                "error": {
                    "message": str(error),
                    "type": "upstream_error",
                    "code": "codebuff_error",
                }
            }
        )
        yield encode_sse("[DONE]")
    finally:
        _schedule_finalize_run(client, run, message_id)
        if account_lease is not None:
            await account_lease.aclose()


async def _collect_completion(
    request: Request,
    payload: dict[str, Any],
    run: FreebuffRun,
    model: str,
    *,
    client: CodebuffClient | None = None,
) -> dict[str, Any]:
    message_id: str | None = None
    accumulator = CompletionAccumulator(model)
    client = client or _client(request)
    try:
        async for line in client.chat_events(payload):
            data = decode_sse_data(line)
            if data is None:
                continue
            if data == "[DONE]":
                break
            message_id = data.get("id") or message_id
            accumulator.add(data)
        response = accumulator.final_response()
        logger.info(
            "chat completion response run_id=%s message_id=%s content_chars=%s finish_reason=%s",
            run.run_id,
            message_id,
            len(response["choices"][0]["message"].get("content") or ""),
            response["choices"][0].get("finish_reason"),
        )
        if _settings(request).debug:
            logger.debug(
                "chat completion response body=%s",
                render_debug(response, _settings(request).log_body_chars),
            )
        return response
    finally:
        await _finalize_run(request, run, message_id, client=client)


async def _start_freebuff_run_chain(
    client: CodebuffClient,
    model: FreebuffModel | str,
) -> FreebuffRun:
    if isinstance(model, str):
        model = FreebuffModel(model, model)
    if model.parent_agent_id:
        return await _start_child_chat_run_chain(client, model)

    agent_id = model.agent_id
    started_at = utc_now_iso()
    run_id = await client.start_run(agent_id)
    child_started_at = utc_now_iso()
    child_run_id = await client.start_run(
        CONTEXT_PRUNER_AGENT_ID,
        ancestor_run_ids=[run_id],
    )
    await client.record_run_step(
        child_run_id,
        step_number=1,
        child_run_ids=[],
        message_id=None,
        start_time=child_started_at,
    )
    await asyncio.gather(
        client.finish_run(child_run_id, total_steps=2),
        client.record_run_step(
            run_id,
            step_number=1,
            child_run_ids=[child_run_id],
            message_id=None,
            start_time=started_at,
        ),
    )
    return FreebuffRun(
        run_id=run_id,
        agent_id=agent_id,
        started_at=started_at,
        child_run_id=child_run_id,
    )


async def _start_child_chat_run_chain(
    client: CodebuffClient,
    model: FreebuffModel,
) -> FreebuffRun:
    assert model.parent_agent_id is not None

    started_at = utc_now_iso()
    parent_run_id = await client.start_run(model.parent_agent_id)
    chat_started_at = utc_now_iso()
    chat_run_id = await client.start_run(
        model.agent_id,
        ancestor_run_ids=[parent_run_id],
    )
    return FreebuffRun(
        run_id=parent_run_id,
        agent_id=model.parent_agent_id,
        started_at=started_at,
        child_run_id=chat_run_id,
        chat_run_id=chat_run_id,
        chat_started_at=chat_started_at,
    )


async def _finalize_run(
    request: Request,
    run: FreebuffRun,
    message_id: str | None,
    *,
    client: CodebuffClient | None = None,
) -> None:
    await _finalize_run_with_client(client or _client(request), run, message_id)


def _schedule_finalize_run(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    task = asyncio.create_task(_finalize_run_with_client(client, run, message_id))

    def _log_background_error(done: asyncio.Task[None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            logger.debug("background finalize task cancelled run_id=%s", run.run_id)
        except Exception:
            logger.exception("background finalize task failed run_id=%s", run.run_id)

    task.add_done_callback(_log_background_error)


async def _finalize_run_with_client(
    client: CodebuffClient,
    run: FreebuffRun,
    message_id: str | None,
) -> None:
    try:
        logger.debug(
            "finalize run start run_id=%s message_id=%s started_at=%s",
            run.run_id,
            message_id,
            run.started_at,
        )
        if run.chat_run_id and run.chat_run_id != run.run_id:
            await client.record_run_step(
                run.chat_run_id,
                step_number=1,
                child_run_ids=[],
                message_id=message_id,
                start_time=run.chat_started_at or run.started_at,
            )
            await client.finish_run(run.chat_run_id, total_steps=2)
            await client.record_run_step(
                run.run_id,
                step_number=1,
                child_run_ids=[run.chat_run_id],
                message_id=None,
                start_time=run.started_at,
            )
            await client.finish_run(run.run_id, total_steps=2)
            logger.debug("finalize parent/child run done run_id=%s", run.run_id)
            return

        await client.record_run_step(
            run.run_id,
            step_number=2,
            child_run_ids=[],
            message_id=message_id,
            start_time=run.started_at,
        )
        await client.finish_run(run.run_id, total_steps=3)
        logger.debug("finalize run done run_id=%s", run.run_id)
    except CodebuffError as error:
        logger.warning(
            "finalize run failed run_id=%s: %s",
            run.run_id,
            error,
            exc_info=client.settings.debug,
        )
    except Exception:
        logger.exception("finalize run failed run_id=%s", run.run_id)
