"""
Logging filter that injects request-scoped identity fields into every log record.

WHY THIS FILE EXISTS
--------------------
Python's ``logging.Filter`` is a hook that runs on every ``LogRecord`` before
it reaches a handler. We use it — instead of passing IDs as extra= arguments
on every logger call — because:

1. **Zero-effort propagation**: Developers call ``logger.info("event")`` with
   no keyword arguments. The filter transparently enriches the record with
   ``request_id``, ``task_id``, and ``trace_id`` from ContextVars. No
   boilerplate in application code.

2. **Correctness in library code**: Third-party libraries (motor, httpx, etc.)
   emit log records too. With the filter approach, even library log records are
   enriched with the current request's IDs — crucial for correlating, e.g., a
   MongoDB timeout with the invoice request that triggered it.

3. **Separation of concerns**: The StructuredLogger knows nothing about
   ContextVars. The formatter knows nothing about ContextVars. Only the filter
   bridges the asyncio context world and the logging world.

PLACEMENT
---------
The filter is attached to the ``StreamHandler`` (not the root logger) because:
- Filters on handlers run only once per record, not once per logger + handler
  combination.
- Handler-level attachment makes it easy to add a second handler (e.g. a file
  handler) that also gets enriched records without registering the filter twice.

RETURN VALUE
------------
``filter()`` always returns True (never drops records). Dropping is controlled
by the logger's level, not by this filter. A filter that sometimes returns
False makes log completeness unpredictable and hard to reason about.
"""

from __future__ import annotations

import logging

from neoflo_logger._config import get_config
from neoflo_logger._context import get_context


class ContextInjectingFilter(logging.Filter):
    """Inject request-scoped IDs and SDK metadata into every LogRecord.

    Attributes injected:
        neoflo_request_id  — from request_id_var ContextVar
        neoflo_task_id     — from task_id_var ContextVar
        neoflo_trace_id    — from trace_id_var ContextVar
        neoflo_service     — from LoggerConfig.service_name (constant per process)
        neoflo_environment — from LoggerConfig.environment (constant per process)

    We prefix all custom attributes with ``neoflo_`` to avoid colliding with
    LogRecord built-in attributes (filename, lineno, funcName, etc.) or
    attributes that other libraries may inject.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # --- Request-scoped IDs (change per request, read from ContextVars) ---
        ctx = get_context()

        # We assign to the record directly rather than using record.__dict__.update()
        # because direct attribute assignment is caught by mypy and linters,
        # while dict updates are invisible to static analysis.
        record.neoflo_request_id = ctx.request_id  # type: ignore[attr-defined]
        record.neoflo_task_id = ctx.task_id  # type: ignore[attr-defined]
        record.neoflo_trace_id = ctx.trace_id  # type: ignore[attr-defined]

        # --- Process-scoped metadata (constant after configure_logging()) ---
        # We read from get_config() on every record rather than caching in
        # __init__ so that tests can call configure_logging() multiple times and
        # the filter picks up the latest config automatically.
        try:
            cfg = get_config()
            record.neoflo_service = cfg.service_name  # type: ignore[attr-defined]
            record.neoflo_environment = cfg.environment  # type: ignore[attr-defined]
        except RuntimeError:
            # Safety valve: if a log record is emitted before configure_logging()
            # (e.g. in a module-level statement during import), we fall back to
            # placeholder values rather than raising and swallowing the record.
            record.neoflo_service = "unconfigured"  # type: ignore[attr-defined]
            record.neoflo_environment = "unknown"  # type: ignore[attr-defined]

        # Always approve — level-based filtering is handled by the logger itself.
        return True
