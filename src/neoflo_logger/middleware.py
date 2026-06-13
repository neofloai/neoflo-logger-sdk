"""
RequestIDMiddleware — ASGI middleware for request correlation and lifecycle logging.

WHY THIS FILE EXISTS
--------------------
Every inbound HTTP request must:

1. **Receive a request_id**: Either propagated from an upstream service via the
   ``X-Request-ID`` header, or freshly generated if the request originates
   externally (user browser, API client). This ID enables cross-service log
   correlation — searching for a single request_id in Coralogix shows every
   log line across all services that handled that user action.

2. **Receive a task_id**: Always generated fresh *within this service*. Even
   when multiple services handle the same request (same request_id), each
   service's internal log lines are distinguished by their task_id. This is
   essential when the same service is called twice in a fan-out pattern.

3. **Store IDs in ContextVars**: asyncio coroutines run on a shared thread pool.
   Thread-local storage would bleed IDs between concurrent requests. ContextVars
   give each async Task its own logical storage slot.

4. **Propagate IDs to response headers**: Downstream services and the frontend
   receive ``X-Request-ID`` and ``X-Task-ID`` headers so they can log the same
   IDs, and developers can use them in curl / browser DevTools for debugging.

5. **Log request lifecycle**: Start and end log lines are emitted automatically
   so operators can see every request's method, path, status code, and duration
   without any additional instrumentation in application code.

WHY STARLETTE NOT FASTAPI
--------------------------
Starlette is the ASGI framework that FastAPI is built on. By depending on
Starlette rather than FastAPI, this middleware is portable to any Starlette-
based service. Starlette is already a transitive dependency of FastAPI, so
adding it to this SDK does not increase the overall dependency footprint.

WHY CALL_NEXT PATTERN
----------------------
Starlette's ``BaseHTTPMiddleware`` wraps ``call_next`` to give us a place to
run code before and after the request handler. We use ``try/finally`` to
guarantee cleanup (ContextVar reset via tokens) even when the handler raises.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from neoflo_logger._context import (
    request_id_var,
    set_request_id,
    set_task_id,
    set_trace_id,
    task_id_var,
    trace_id_var,
)
from neoflo_logger._logger import StructuredLogger
from neoflo_logger._otel import get_current_trace_id

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# X-Request-ID must be alphanumeric + hyphens, 1-64 characters.
# This regex rejects nulls, slashes, and injection payloads while being
# permissive enough for UUID4 strings (36 chars), short IDs, and custom formats.
_REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9\-]{1,64}$")

# Logger for middleware-internal log lines. Uses __name__ so the source
# appears as "neoflo_logger.middleware" in the ``file`` field — distinguishable
# from application log lines.
_logger = StructuredLogger(__name__)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that manages request/task IDs for every HTTP request.

    Attach to a FastAPI or Starlette application at startup:

        from neoflo_logger.middleware import RequestIDMiddleware
        app.add_middleware(RequestIDMiddleware)

    After this middleware runs:
    - ``request_id_var`` holds the validated/generated request ID.
    - ``task_id_var`` holds a fresh UUID4 for this request.
    - ``trace_id_var`` holds the OTel trace_id (if a span is active).
    - The response carries ``X-Request-ID`` and ``X-Task-ID`` headers.

    Args:
        app: The ASGI application to wrap. Passed automatically by Starlette.
        log_requests: If True (default), emit INFO log lines at request start
                      and completion. Set to False if you use a separate access
                      log handler.
    """

    def __init__(self, app: ASGIApp, *, log_requests: bool = True) -> None:
        super().__init__(app)
        self._log_requests = log_requests

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process one HTTP request end-to-end.

        Execution order:
            1. Extract / validate / generate request_id
            2. Generate task_id
            3. Write both to ContextVars (asyncio-safe)
            4. Read OTel trace_id from active span (if any)
            5. Log request start
            6. Forward to the actual request handler
            7. Log request completion with timing
            8. Add correlation headers to response
            9. Reset ContextVars to their previous values

        The ContextVar reset in step 9 uses the reset tokens returned by
        ``set_*()`` calls. This restores the *previous* value rather than
        clearing to the default — important for nested middleware stacks where
        an outer middleware may have already set context values.
        """
        # --- Step 1: Request ID ---
        raw_request_id = request.headers.get("X-Request-ID", "")
        if _REQUEST_ID_RE.match(raw_request_id):
            # Valid upstream ID — propagate as-is for cross-service correlation.
            request_id = raw_request_id
        else:
            # Invalid or missing — generate a fresh UUID4.
            # We generate even when the header is present but invalid (e.g. SQL
            # injection attempt) rather than accepting the malformed value.
            request_id = str(uuid.uuid4())

        # --- Step 2: Task ID — always fresh, never propagated ---
        task_id = str(uuid.uuid4())

        # --- Step 3: Write to ContextVars ---
        # We capture the reset tokens so we can restore previous values in the
        # finally block. Using token.reset() is safer than calling set("") or
        # set("-") because it handles nested middleware correctly.
        rid_token = set_request_id(request_id)
        tid_token = set_task_id(task_id)

        # --- Step 4: OTel trace_id ---
        # We read the trace_id *after* setting request/task IDs because
        # some OTel instrumentation (e.g. opentelemetry-instrumentation-fastapi)
        # creates the root span inside the middleware chain. If no span is
        # active yet, get_current_trace_id() returns "-" (harmless default).
        trace_id = get_current_trace_id()
        trid_token = set_trace_id(trace_id)

        # --- Timing start ---
        start_time = time.perf_counter()

        if self._log_requests:
            _logger.info(
                "request_started",
                data={
                    "method": request.method,
                    "path": str(request.url.path),
                    "query": str(request.url.query) or None,
                    "client_ip": _get_client_ip(request),
                },
            )

        try:
            # --- Step 6: Forward to handler ---
            response: Response = await call_next(request)

            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)

            if self._log_requests:
                _logger.info(
                    "request_completed",
                    data={
                        "method": request.method,
                        "path": str(request.url.path),
                        "status_code": response.status_code,
                        "duration_ms": duration_ms,
                    },
                )

            # --- Step 8: Propagate IDs to response headers ---
            # Downstream services and the browser DevTools can read these
            # headers to correlate their own log searches.
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Task-ID"] = task_id

            return response

        except Exception:
            # If the handler raises (unhandled 500), we still want the
            # timing and ID in the log. We re-raise after logging so the
            # exception propagates to Starlette's error handler.
            duration_ms = round((time.perf_counter() - start_time) * 1000, 2)
            _logger.error(
                "request_unhandled_exception",
                data={
                    "method": request.method,
                    "path": str(request.url.path),
                    "duration_ms": duration_ms,
                },
                exc_info=True,
            )
            raise

        finally:
            # --- Step 9: Reset ContextVars ---
            # This is critical: without resetting, a reused worker coroutine
            # (e.g. from uvicorn's thread pool) would carry stale IDs into
            # the next request it handles.
            #
            # We import the ContextVar objects directly here (rather than via the
            # set_* helpers) because reset() must be called on the ContextVar
            # itself, not on the wrapper function. The vars are already imported
            # at the top of the dispatch() scope via the set_* calls above.
            request_id_var.reset(rid_token)
            task_id_var.reset(tid_token)
            trace_id_var.reset(trid_token)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Extract the real client IP, accounting for reverse proxy headers.

    We check ``X-Forwarded-For`` first because AWS ALB sets it when the
    request passes through the load balancer. The leftmost IP in the header
    is the original client; subsequent IPs are intermediary proxies.

    Falling back to ``request.client.host`` covers direct connections
    (development, internal services bypassing the ALB).
    """
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP only — the rest are proxy IPs added by the chain.
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None
