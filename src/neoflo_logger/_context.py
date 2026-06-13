"""
Context variable definitions for cross-cutting request identity fields.

WHY THIS FILE EXISTS
--------------------
In an async Python application (FastAPI / asyncio), multiple requests are
processed concurrently within a single OS thread. Traditional thread-local
storage (threading.local) does NOT work in async code because one thread
services many coroutines. ContextVar solves this: each asyncio Task gets its
own logical "slot" for each ContextVar, so setting ``request_id_var`` in one
request's coroutine does not bleed into another request's coroutine running
on the same thread.

This file is intentionally thin — it defines *only* the ContextVars and
exposes typed helpers to get/set them. All other modules import from here;
no module should define its own ContextVar for these fields, as duplicate
definitions would create independent slots that never share values.

USAGE
-----
The RequestIDMiddleware writes to these vars at the start of every request.
The _filter.py Filter reads them on every log record emission.
The StructuredLogger never touches them directly — it just emits log records
and the filter enriches them automatically.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Core ContextVars
# ---------------------------------------------------------------------------

# request_id — propagated from upstream services via the X-Request-ID header.
# Purpose: correlate a single user action *across* multiple microservices.
# Default "-" (not None) so that log records always have a printable value even
# when logging happens outside of a request context (e.g. startup tasks).
request_id_var: ContextVar[str] = ContextVar("neoflo.request_id", default="-")

# task_id — generated fresh *inside this service* for every inbound request.
# Purpose: trace work *within* this process only.
# Different from request_id: even if the same upstream request triggers two
# parallel calls to this service, each gets its own task_id so log lines from
# the two concurrent executions are distinguishable.
task_id_var: ContextVar[str] = ContextVar("neoflo.task_id", default="-")

# trace_id — extracted from the active OpenTelemetry span's context.
# We store it in a ContextVar rather than reading the OTel context on every
# log call because: (1) OTel span context may not be accessible in all
# coroutine frames, and (2) caching here keeps the formatter simple and
# allocation-free on the hot path.
# The middleware sets this after creating/propagating the trace context.
trace_id_var: ContextVar[str] = ContextVar("neoflo.trace_id", default="-")


# ---------------------------------------------------------------------------
# Typed accessors
# ---------------------------------------------------------------------------
# These functions centralise all reads/writes to the ContextVars.
# They are the only sanctioned way to interact with context state — importing
# the ContextVar objects directly and calling .set()/.get() elsewhere is
# discouraged because it bypasses the type safety and future-proofing here.


class RequestContext(NamedTuple):
    """Snapshot of all context IDs active for the current request.

    Returned by ``get_context()`` so callers can read all three IDs in one
    call without three separate ContextVar lookups. Using NamedTuple keeps
    it lightweight (no heap allocation beyond the tuple itself).
    """

    request_id: str
    task_id: str
    trace_id: str


def get_context() -> RequestContext:
    """Return all current context IDs as an immutable snapshot."""
    return RequestContext(
        request_id=request_id_var.get(),
        task_id=task_id_var.get(),
        trace_id=trace_id_var.get(),
    )


def set_request_id(value: str) -> Token[str]:
    """Set the current request's correlation ID and return the reset token.

    The returned Token allows callers to restore the previous value via
    ``request_id_var.reset(token)`` — important in middleware where we need
    to clean up context after the response is sent.
    """
    return request_id_var.set(value)


def set_task_id(value: str) -> Token[str]:
    """Set the current request's task ID and return the reset token."""
    return task_id_var.set(value)


def set_trace_id(value: str) -> Token[str]:
    """Set the current trace ID (extracted from OTel span) and return the reset token."""
    return trace_id_var.set(value)
