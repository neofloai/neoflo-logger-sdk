"""
neoflo_logger — Neoflo platform structured logging SDK.

OVERVIEW
--------
This package provides a single, consistent logging interface for all Neoflo
Python microservices. It replaces ad-hoc calls to the stdlib ``logging`` module
with a structured, opinionated wrapper that:

  - Emits JSON-formatted log lines to stdout (ingested by CloudWatch / ECS)
  - Simultaneously ships telemetry to the OTEL collector via gRPC (→ Coralogix)
  - Auto-injects request_id, task_id, trace_id, service, and environment into
    every log record with zero application-code boilerplate
  - Redacts sensitive fields (password, token, secret, key, hash, authorization)
  - Integrates with FastAPI via ``RequestIDMiddleware``

CANONICAL LOG ENVELOPE
----------------------
Every log line is a single JSON object on stdout:

    {
        "timestamp": "2026-06-13T15:16:11.543Z",   # ISO 8601 UTC with ms
        "level": "INFO",                            # DEBUG / INFO / WARNING / ERROR / CRITICAL
        "service": "invoice-validator-be",          # set at configure_logging()
        "environment": "production",                # dev / staging / production
        "request_id": "abc-123",                    # from X-Request-ID header (or generated)
        "task_id": "def-456",                       # fresh UUID4 per request
        "trace_id": "5d2410e7773bcfcc...",          # from active OTel span
        "file": "invoice_service.py",               # caller's filename
        "function": "fetch_invoice",                # caller's function
        "line": 102,                                # caller's line number
        "label": "invoice_fetched",                 # first arg to logger.info()
        "data": {"invoice_id": "INV-002"}           # arbitrary dict (optional)
    }

QUICK START
-----------
In your FastAPI service's ``main.py`` or ``app.py``:

    import os
    from fastapi import FastAPI
    from neoflo_logger import configure_logging, get_logger
    from neoflo_logger.middleware import RequestIDMiddleware

    configure_logging(
        service_name="invoice-validator-be",
        otlp_endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
        environment=os.environ.get("ENVIRONMENT", "dev"),
        debug=os.environ.get("DEBUG", "false").lower() == "true",
    )

    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

In any other module:

    from neoflo_logger import get_logger
    logger = get_logger(__name__)

    logger.info("invoice_fetched", data={"invoice_id": "INV-002", "duration_ms": 71.8})
    logger.warning("invoice_not_found", data={"invoice_id": "bad-001"})
    logger.error("db_connection_failed", data={"host": "mongo", "retry": 3})
    logger.debug("token_decoded", data={"user_id": "abc"})    # suppressed in production
    logger.critical("service_unavailable", data={"dependency": "n8n"})

PUBLIC API
----------
This ``__init__.py`` exports only the symbols that application code needs.
Internal implementation modules (``_config``, ``_context``, ``_filter``,
``_formatter``, ``_logger``, ``_otel``) are prefixed with ``_`` to signal that
they are private to the SDK. Application code should never import from them
directly — the public API here is the stable contract.
"""

from __future__ import annotations

import logging
import sys

from neoflo_logger._config import LoggerConfig, get_config, is_configured, set_config
from neoflo_logger._filter import ContextInjectingFilter
from neoflo_logger._formatter import JsonFormatter
from neoflo_logger._logger import StructuredLogger
from neoflo_logger._otel import setup_otel

__version__ = "0.1.0"
__all__ = [
    "configure_logging",
    "get_logger",
    "__version__",
]


def configure_logging(
    service_name: str,
    otlp_endpoint: str,
    environment: str,
    debug: bool = False,
) -> None:
    """Bootstrap the Neoflo logging SDK. Call exactly once at application startup.

    This function:
    1. Validates and stores configuration in a frozen dataclass singleton.
    2. Configures the root Python logger with JsonFormatter + ContextInjectingFilter.
    3. Optionally bootstraps the OpenTelemetry trace provider (if otlp_endpoint != "").
    4. Silences known-noisy third-party loggers so they don't pollute log output.

    Calling this function more than once is a no-op after the first call. This
    is intentional: in test environments where ``configure_logging()`` may be
    called multiple times across test cases, subsequent calls should not reset
    the handler chain (which would double-log). Use ``_config._reset_config()``
    + call again when you genuinely need to reconfigure (tests only).

    Args:
        service_name: Stable identifier for this microservice. Appears as
            ``service`` in every log record. Use the ECS task definition name
            (e.g. "invoice-validator-be", "file-ingestion-service").
        otlp_endpoint: gRPC endpoint for the OTEL collector.
            Format: "http://host:4317". Pass "" to disable OTLP export
            (recommended for local development to avoid connection errors).
        environment: Deployment environment. One of "dev", "staging", "production".
            Controls OTLP export and log-level defaults.
        debug: When True, sets min log level to DEBUG. Should be False in
            staging and production to avoid logging sensitive internal state.

    Raises:
        ValueError: If service_name is empty (a common misconfiguration).
    """
    if not service_name or not service_name.strip():
        raise ValueError(
            "service_name must be a non-empty string. "
            "Use the ECS task definition name (e.g. 'invoice-validator-be')."
        )

    # Guard: if already configured, skip silently to support module re-imports.
    if is_configured():
        return

    # --- Build config ---
    min_level = logging.DEBUG if debug else logging.INFO
    enable_otlp = bool(otlp_endpoint)

    config = LoggerConfig(
        service_name=service_name.strip(),
        otlp_endpoint=otlp_endpoint,
        environment=environment,
        debug=debug,
        min_level=min_level,
        enable_otlp=enable_otlp,
    )
    set_config(config)

    # --- Wire up the root logger ---
    # We configure the *root* logger (not a named logger) so that all loggers
    # in the process — including third-party libraries — emit JSON through our
    # handler. This guarantees a uniform log format across the entire service.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextInjectingFilter())

    root = logging.getLogger()
    # Clear any existing handlers (e.g. default StreamHandler added by uvicorn
    # or pytest's log capture). We own the handler chain from here on.
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(min_level)

    # --- Silence known-noisy third-party loggers ---
    # These libraries emit DEBUG/INFO records for every internal operation
    # (connection pool events, DNS lookups, etc.) that would flood production
    # logs. We set them to WARNING so only genuine issues surface.
    # Add new entries here when a newly integrated library turns out to be noisy.
    _NOISY_LOGGERS = (
        "motor",            # MongoDB async driver — logs every pool event
        "pymongo",          # MongoDB sync driver
        "uvicorn.access",   # HTTP access log (we have RequestIDMiddleware instead)
        "httpcore",         # low-level HTTP transport used by httpx
        "httpx",            # HTTP client used by inter-service calls
        "botocore",         # AWS SDK — very verbose in DEBUG
        "boto3",            # AWS SDK higher-level
        "aiobotocore",      # async AWS SDK
        "opentelemetry",    # OTel internal logs can be cyclically noisy
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    # --- Bootstrap OpenTelemetry (if endpoint configured) ---
    if enable_otlp:
        setup_otel(
            service_name=config.service_name,
            otlp_endpoint=config.otlp_endpoint,
            environment=config.environment,
        )


def get_logger(name: str) -> StructuredLogger:
    """Return a StructuredLogger for the given module name.

    Intended to be called once at module level:

        logger = get_logger(__name__)

    Passing ``__name__`` (the Python module name) as ``name`` is idiomatic
    and mirrors the stdlib pattern. It enables per-module log level overrides
    via the standard logging configuration API if needed.

    Note: ``configure_logging()`` does NOT need to be called before
    ``get_logger()`` — the StructuredLogger lazily reads config on each emit.
    However, log records emitted before ``configure_logging()`` will not have
    a handler attached and will be silently discarded (Python's default
    behaviour for the root logger with no handlers).

    Args:
        name: Typically ``__name__`` of the calling module.

    Returns:
        A StructuredLogger instance. All instances sharing the same ``name``
        are backed by the same ``logging.Logger`` object (Python's internal cache).
    """
    return StructuredLogger(name)
