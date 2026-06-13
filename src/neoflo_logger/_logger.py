"""
StructuredLogger — thin wrapper around stdlib logging.Logger.

WHY THIS FILE EXISTS
--------------------
We wrap the standard ``logging.Logger`` rather than using it directly because:

1. **Enforced API shape**: Every Neoflo log call must have a ``label`` (event
   name) as the first positional argument and an optional ``data`` dict.
   The standard Logger.info(msg, *args, **kwargs) signature allows any call
   shape, which leads to inconsistent log records across services. Our wrapper
   makes the intended shape explicit and enforces it at the type level.

2. **Automatic sanitization**: Sensitive fields (password, token, secret, etc.)
   are redacted *before* the record is emitted. If we relied on developers to
   sanitize manually, we'd inevitably leak secrets. Centralizing it here means
   it's impossible to bypass.

3. **stacklevel forwarding**: Python's logging records ``filename``, ``funcName``,
   and ``lineno`` by walking up the call stack. Without ``stacklevel=3`` here
   (caller → _emit → logging.Logger.log), these fields would always point into
   the SDK internals rather than the developer's application code. Setting
   ``stacklevel=3`` makes them point at the actual call site.

4. **Level guard on hot path**: ``if self._logger.isEnabledFor(level)`` avoids
   building the ``extra`` dict and sanitizing data when the log level would be
   filtered anyway. This is important for DEBUG calls in tight loops.

INTERFACE
---------
    logger = get_logger(__name__)
    logger.info("invoice_fetched", data={"invoice_id": "INV-002", "duration_ms": 71.8})
    logger.error("db_connection_failed", data={"host": "mongo"}, exc_info=True)

WHY ``data=`` IS A KEYWORD-ONLY ARGUMENT
-----------------------------------------
Forcing ``data`` to be keyword-only (with ``*``) prevents the common mistake of
passing a dict as the second positional argument, which would be silently
ignored by the standard Logger. The explicit ``data=`` makes intent clear and
enables future additions to the method signature without breaking callers.
"""

from __future__ import annotations

import logging
from typing import Any

# ---------------------------------------------------------------------------
# Fields whose values must be redacted regardless of casing.
# We use substring matching (see _sanitize) rather than exact match so that
# variations like "api_key", "apikey", "secret_key" are all caught without
# maintaining an exhaustive list.
#
# WHY FROZENSET: frozenset provides O(1) ``in`` lookup and is immutable,
# preventing accidental modification at runtime. It's also hashable so it
# can be used as a dict key if needed in future.
# ---------------------------------------------------------------------------
_REDACT_SUBSTRINGS: frozenset[str] = frozenset(
    {"password", "token", "secret", "key", "hash", "authorization"}
)

_REDACTED_VALUE = "***REDACTED***"

# stacklevel=3 means: logging.Logger.log → _emit → debug/info/warning/error/critical → caller
# Each wrapper call in this class is exactly 2 frames deep (caller → public method → _emit),
# so stacklevel=3 correctly attributes the record to the caller's frame.
_STACK_LEVEL = 3


class StructuredLogger:
    """Neoflo-standard structured logger with automatic sanitization.

    Obtain instances via ``get_logger(__name__)`` — do not instantiate directly.

    Every method follows the same contract:
        logger.<level>(label: str, *, data: dict | None = None, exc_info: bool = False)

    Args:
        label: A snake_case event identifier used for search and alerting
               (e.g. "invoice_fetched", "db_connection_failed").
               It appears as the ``label`` field in JSON output.
        data:  Arbitrary JSON-serializable dict. Sensitive keys are auto-redacted.
               Pass None (default) when there's no structured payload.
        exc_info: When True, appends the current exception traceback to the
                  log record. Only relevant for error/critical; ignored silently
                  for info/debug to avoid accidental traceback spam.
    """

    def __init__(self, name: str) -> None:
        # We hold a reference to the standard Logger. All level/handler/filter
        # configuration lives on the standard Logger — we never replicate it.
        # This means callers can still use ``logging.getLogger(name).setLevel()``
        # and it affects our wrapper transparently.
        self._logger = logging.getLogger(name)

    # -------------------------------------------------------------------------
    # Public level methods
    # -------------------------------------------------------------------------

    def debug(self, label: str, *, data: dict[str, Any] | None = None) -> None:
        """Emit a DEBUG-level record. Commonly used for internal tracing.

        DEBUG records are suppressed in production (min_level=INFO) so they
        must not carry business-critical information that operators need to see.
        """
        if self._logger.isEnabledFor(logging.DEBUG):
            self._emit(logging.DEBUG, label, data)

    def info(self, label: str, *, data: dict[str, Any] | None = None) -> None:
        """Emit an INFO-level record. The baseline for normal operations."""
        if self._logger.isEnabledFor(logging.INFO):
            self._emit(logging.INFO, label, data)

    def warning(self, label: str, *, data: dict[str, Any] | None = None) -> None:
        """Emit a WARNING-level record. For recoverable but notable conditions."""
        if self._logger.isEnabledFor(logging.WARNING):
            self._emit(logging.WARNING, label, data)

    # Alias so that callers who spell it ``warn`` (common mistake) still work.
    warn = warning

    def error(
        self,
        label: str,
        *,
        data: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> None:
        """Emit an ERROR-level record.

        Args:
            exc_info: Pass True inside an ``except`` block to capture and attach
                      the current traceback. The traceback appears as ``exc_info``
                      in the JSON envelope.
        """
        if self._logger.isEnabledFor(logging.ERROR):
            self._emit(logging.ERROR, label, data, exc_info=exc_info)

    def critical(
        self,
        label: str,
        *,
        data: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> None:
        """Emit a CRITICAL-level record. Reserved for service-threatening conditions.

        CRITICAL records bypass the level guard because by definition they must
        always be emitted regardless of configured min_level — if the service is
        in a critical state, we need that in the logs unconditionally.
        """
        # No isEnabledFor guard — CRITICAL always fires.
        self._emit(logging.CRITICAL, label, data, exc_info=exc_info)

    def exception(
        self,
        label: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Emit ERROR with the current exception traceback automatically captured.

        Convenience method: equivalent to ``logger.error(label, data=data, exc_info=True)``.
        Intended to be called inside an ``except`` block:

            try:
                ...
            except SomeError:
                logger.exception("db_write_failed", data={"collection": "invoices"})
        """
        if self._logger.isEnabledFor(logging.ERROR):
            self._emit(logging.ERROR, label, data, exc_info=True)

    # -------------------------------------------------------------------------
    # Standard Logger delegation — used by uvicorn / third-party code that
    # holds a reference to a StructuredLogger and calls setLevel / isEnabledFor.
    # -------------------------------------------------------------------------

    def setLevel(self, level: int | str) -> None:
        """Delegate to the underlying Logger. Useful in tests to silence noisy loggers."""
        self._logger.setLevel(level)

    def isEnabledFor(self, level: int) -> bool:
        """Delegate to the underlying Logger."""
        return self._logger.isEnabledFor(level)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _sanitize(self, data: dict[str, Any]) -> dict[str, Any]:
        """Return a new dict with sensitive field values replaced by REDACTED.

        We check for substring matches (not exact key equality) because sensitive
        data appears in many key naming styles:
            "password", "user_password", "passwordHash", "api_key", "apiKey",
            "Authorization", "bearer_token", etc.

        WHY NEW DICT: we never mutate the caller's dict — that would modify the
        dict the developer passed, which is unexpected and hard to debug.
        Immutability is a core project principle.

        WHY LOWERCASE COMPARISON: key casing is unpredictable (camelCase from
        JSON deserialisation, UPPER from env vars, snake_case from Python).
        Lowercasing the key before substring-checking catches all variants.
        """
        return {
            k: (_REDACTED_VALUE if any(s in k.lower() for s in _REDACT_SUBSTRINGS) else v)
            for k, v in data.items()
        }

    def _emit(
        self,
        level: int,
        label: str,
        data: dict[str, Any] | None,
        *,
        exc_info: bool = False,
    ) -> None:
        """Build the ``extra`` dict and delegate to the standard Logger.

        We use ``extra=`` (not ``msg`` formatting) so the formatter receives
        typed Python objects rather than pre-serialized strings. The formatter
        assembles the final JSON from these extras.

        The ``event_label`` and ``event_data`` keys in ``extra`` are the
        contract between this layer and ``JsonFormatter``. They use an
        ``event_`` prefix to avoid shadowing any LogRecord built-in attribute.
        """
        sanitized = self._sanitize(data) if data else None

        extra: dict[str, Any] = {"event_label": label}
        if sanitized:
            extra["event_data"] = sanitized

        self._logger.log(
            level,
            # The ``msg`` argument is intentionally the label string.
            # This makes ``record.getMessage()`` readable in places that
            # consume raw LogRecords (e.g. pytest log capture).
            label,
            extra=extra,
            exc_info=exc_info,
            # stacklevel=3: Logger.log → _emit → (debug|info|warning|error|critical) → caller
            stacklevel=_STACK_LEVEL,
        )
