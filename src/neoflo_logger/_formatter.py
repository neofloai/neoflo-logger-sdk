"""
JSON formatter that produces the Neoflo standard log envelope.

WHY THIS FILE EXISTS
--------------------
Python's default ``logging.Formatter`` produces human-readable text. In
production on ECS / CloudWatch, every log line is ingested by a log aggregator
(here: the OTEL collector → Coralogix). Structured JSON is mandatory so that:

1. **Field-level search**: CloudWatch Insights can query ``$.request_id``
   directly. Text logs require regex which is slow and brittle.
2. **Consistent schema**: Every microservice emits the identical envelope so
   Coralogix dashboards, alerts, and saved queries work across all services
   without per-service customization.
3. **Automatic parsing**: The OTEL collector's ``json_parser`` operator can
   decode structured JSON in a single pass with no regex.

DESIGN DECISIONS
----------------
- ``json.dumps(default=str)`` is the serialization fallback for any value
  that isn't natively JSON-serializable (e.g. Decimal, datetime, ObjectId).
  Using ``str()`` as the fallback is safe and predictable; it avoids raising
  ``TypeError`` on the hot path.
- Exception formatting (``exc_info``) is appended as a plain string under
  ``exc_info`` rather than a structured dict because Python tracebacks are
  inherently unstructured text. Parsing them further is left to the log
  aggregator.
- We catch all exceptions inside ``format()`` and emit a fallback JSON line
  rather than letting the formatter raise. A formatter that throws silently
  drops the log record (Python's logging machinery catches formatter errors
  and does nothing). A fallback JSON line at least lets the operator know
  something went wrong.

ENVELOPE SCHEMA
---------------
See module-level docstring in __init__.py for the full canonical schema.
"""

from __future__ import annotations

import datetime
import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    """Serialize every LogRecord to a single-line JSON string.

    The formatter reads custom attributes injected by ``ContextInjectingFilter``
    (``neoflo_*`` prefixed) and standard LogRecord attributes (filename, lineno,
    funcName) to assemble the envelope.

    It does NOT know about ContextVars — the filter is responsible for reading
    those and attaching them to the record before the formatter runs.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Convert a LogRecord to the canonical Neoflo JSON envelope.

        Args:
            record: The log record, already enriched by ContextInjectingFilter.

        Returns:
            A single-line JSON string (no trailing newline — the handler adds one).
        """
        try:
            return self._build_json(record)
        except Exception as exc:
            # Last-resort fallback: emit a minimal JSON line so the operator
            # can see that something went wrong with the formatter itself.
            # We write to stderr separately so this doesn't get swallowed.
            sys.stderr.write(f"[neoflo_logger formatter error] {exc}\n")
            return json.dumps(
                {
                    "level": record.levelname,
                    "label": "_neoflo_formatter_error",
                    "data": {
                        "error": str(exc),
                        "fallback_message": record.getMessage(),
                    },
                }
            )

    def _build_json(self, record: logging.LogRecord) -> str:
        """Build the envelope dict and serialize it. Called by format()."""

        # --- Timestamp ---
        # We format as ISO 8601 UTC with milliseconds and "Z" suffix.
        # Using datetime.fromtimestamp(tz=utc) is correct regardless of the
        # server's local timezone (never assume UTC on the host).
        ts_base = datetime.datetime.fromtimestamp(
            record.created, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S")
        timestamp = f"{ts_base}.{int(record.msecs):03d}Z"

        # --- Label (event name) ---
        # StructuredLogger stores the caller's first argument as ``event_label``
        # in extra=. If missing (e.g. a library using standard logging), fall
        # back to the raw message.
        label: str = getattr(record, "event_label", None) or record.getMessage()

        # --- Data payload ---
        # StructuredLogger stores the sanitized data dict as ``event_data``.
        # May be None / empty for label-only log calls.
        data: dict[str, Any] | None = getattr(record, "event_data", None)

        # --- Context IDs (written by ContextInjectingFilter) ---
        request_id: str = getattr(record, "neoflo_request_id", "-")
        task_id: str = getattr(record, "neoflo_task_id", "-")
        trace_id: str = getattr(record, "neoflo_trace_id", "-")
        service: str = getattr(record, "neoflo_service", "unconfigured")
        environment: str = getattr(record, "neoflo_environment", "unknown")

        # --- Assemble envelope ---
        # Field ordering is intentional: identity/correlation fields first,
        # then source location, then payload. This ordering makes the most
        # critical debugging fields appear first in truncated log previews.
        envelope: dict[str, Any] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "service": service,
            "environment": environment,
            "request_id": request_id,
            "task_id": task_id,
            "trace_id": trace_id,
            "file": record.filename,
            "function": record.funcName,
            "line": record.lineno,
            "label": label,
        }

        # --- Optional fields — only include when present to keep JSON lean ---
        if data:
            envelope["data"] = data

        if record.exc_info:
            # formatException returns a multi-line string. We store it as-is;
            # the log aggregator can split on "\n" if needed.
            envelope["exc_info"] = self.formatException(record.exc_info)

        if record.stack_info:
            envelope["stack_info"] = self.formatStack(record.stack_info)

        # ``default=str`` converts any non-JSON-serializable value (datetime,
        # Decimal, ObjectId, Enum, etc.) to its str() representation.
        # This is a conscious trade-off: we lose type fidelity for exotic types,
        # but we never raise ``TypeError`` in the formatter.
        return json.dumps(envelope, default=str)
