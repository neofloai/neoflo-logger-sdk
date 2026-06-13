"""
Global SDK configuration state.

WHY THIS FILE EXISTS
--------------------
``configure_logging()`` is called once at application startup. All subsequent
calls to ``get_logger()`` and middleware instantiation need to read the same
configuration (service_name, environment, etc.) without threading config
through every function call signature.

We store configuration in a module-level singleton (``_config``) rather than
using class-level state, environment variables, or a global dict because:

1. **Type safety**: a frozen dataclass gives us mypy-verified attribute access.
2. **Immutability**: ``frozen=True`` prevents accidental mutation after setup.
   If a module changes ``service_name`` mid-flight, every log line becomes
   incorrect. The frozen dataclass makes that impossible at runtime.
3. **Single source of truth**: one module owns the config object. Every other
   module imports ``get_config()`` from here — there's no risk of two
   different parts of the codebase holding diverged copies.
4. **Testability**: tests can call ``_reset_config()`` to clear state between
   test cases without monkey-patching environment variables.

THREAD / COROUTINE SAFETY
--------------------------
``_config`` is written once during startup (before any requests arrive) and
then only read. Python's GIL makes a single assignment atomic, so concurrent
reads after startup are safe without locking. We explicitly prohibit calling
``configure_logging()`` more than once in production to enforce this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# ---------------------------------------------------------------------------
# Type alias for valid log level strings
# ---------------------------------------------------------------------------

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
Environment = Literal["dev", "staging", "production"]


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoggerConfig:
    """Immutable configuration snapshot for the entire logging SDK.

    All fields are set once at startup via ``configure_logging()`` and never
    mutated afterwards. This design is deliberate: changing log configuration
    at runtime while requests are in flight leads to inconsistent log metadata
    that makes debugging extremely difficult.

    Attributes:
        service_name: Human-readable identifier for this microservice.
            Appears as ``service`` in every log record. Must be stable across
            deployments (e.g. "invoice-validator-be", not "iv-be-v2").
        otlp_endpoint: gRPC endpoint for the OpenTelemetry collector.
            Format: "http://host:4317" (no trailing slash).
            Set to empty string "" to disable OTLP export (useful in dev).
        environment: One of "dev", "staging", "production".
            Appears as ``environment`` in every log record and controls
            whether the OTLP exporter is enabled.
        debug: When True, sets root logger to DEBUG and enables verbose
            internal SDK logging. Should be False in staging/production.
        min_level: Minimum log level to emit. Derived from ``debug`` if not
            explicitly provided. Stored as the Python int constant
            (logging.DEBUG = 10, logging.INFO = 20, etc.) for fast comparison.
        enable_otlp: Whether to send logs/traces to the OTEL collector.
            Automatically False when otlp_endpoint is empty.
    """

    service_name: str
    otlp_endpoint: str
    environment: str  # kept as plain str to accept arbitrary env strings
    debug: bool
    min_level: int  # logging.DEBUG / logging.INFO / etc.
    enable_otlp: bool


# ---------------------------------------------------------------------------
# Module-level singleton — None until configure_logging() is called
# ---------------------------------------------------------------------------

_config: LoggerConfig | None = None


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------


def get_config() -> LoggerConfig:
    """Return the active LoggerConfig.

    Raises RuntimeError if ``configure_logging()`` has not been called yet.
    We raise rather than returning a default so that misconfigured services
    fail loudly at import time, not silently with wrong metadata in logs.
    """
    if _config is None:
        raise RuntimeError(
            "neoflo_logger has not been configured. "
            "Call configure_logging() at application startup before importing get_logger()."
        )
    return _config


def set_config(config: LoggerConfig) -> None:
    """Install a new LoggerConfig. Called exclusively by configure_logging().

    This is a module-level function (not a method on LoggerConfig) to make
    the write path explicitly visible and grep-able. Searching the codebase
    for ``set_config(`` immediately reveals the one place config is mutated.
    """
    global _config  # noqa: PLW0603 — intentional singleton write
    _config = config


def _reset_config() -> None:
    """Reset config to None. FOR TESTING ONLY — do not call in production.

    Allows test cases to call configure_logging() multiple times without
    triggering the "already configured" guard.
    """
    global _config  # noqa: PLW0603
    _config = None


def is_configured() -> bool:
    """Return True if configure_logging() has already been called."""
    return _config is not None
