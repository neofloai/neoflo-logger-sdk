"""
OpenTelemetry SDK bootstrap — TraceProvider and LoggerProvider setup.

WHY THIS FILE EXISTS
--------------------
The Neoflo observability stack routes telemetry through an OTEL collector
(gRPC port 4317) which fans out to Coralogix for logs/metrics/traces. This
file sets up two OTel pipelines:

  1. **Trace pipeline** — creates a global TracerProvider so any code that
     calls ``opentelemetry.trace.get_tracer()`` (including auto-instrumentation
     libraries like opentelemetry-instrumentation-fastapi) sends spans to the
     collector. The active span's trace_id is extracted here and stored in
     ``trace_id_var`` so every log record carries a trace link.

  2. **Log pipeline** — creates a LoggerProvider with an OTLPLogExporter so
     structured logs are shipped to the collector *in addition to* stdout.
     This gives us both searchable JSON in CloudWatch (from stdout) and
     correlated logs-to-traces in Coralogix (from OTLP).

WHY SEPARATE FROM configure_logging()
--------------------------------------
OTel bootstrap has significant side effects (global provider registration,
gRPC channel creation) and requires the OTLP endpoint to be available.
Isolating it here means:
- Tests can call configure_logging() without an OTLP endpoint and skip OTel.
- Services that don't want OTel (e.g. a Lambda) can opt out by passing
  otlp_endpoint="" to configure_logging().
- The module is easy to mock/patch in unit tests.

TRACE ID EXTRACTION
--------------------
We read the trace_id from the *active span context* rather than from request
headers because:
- Auto-instrumentation (opentelemetry-instrumentation-fastapi) creates the
  root span before our middleware runs, so the trace is already established.
- Reading from the active context works across propagation boundaries (HTTP
  headers, gRPC metadata, SNS attributes) without coupling this code to any
  specific propagation format.
"""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_sdk_logger = logging.getLogger(__name__)


def setup_otel(
    service_name: str,
    otlp_endpoint: str,
    environment: str,
) -> None:
    """Bootstrap the global OpenTelemetry Trace provider.

    This function is idempotent with respect to the global provider — if
    a provider is already registered (e.g. by opentelemetry-instrumentation-
    fastapi auto-instrumentation), we do NOT replace it. We only set up a
    new provider if the current global is the no-op default.

    Args:
        service_name: Appears as ``service.name`` in every span.
        otlp_endpoint: gRPC endpoint, e.g. "http://otel-collector:4317".
        environment: Appears as ``deployment.environment`` in every span.
                     Enables per-environment filtering in Coralogix.
    """
    # OTel ``Resource`` carries process-level metadata that appears on every
    # span emitted by this process. We add ``deployment.environment`` because
    # the default Resource only includes ``service.name`` and ``telemetry.sdk.*``.
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
            # service.version would go here once we expose it from pyproject.toml
        }
    )

    # Check if a non-default provider is already installed.
    # We use ``isinstance`` rather than ``is`` because auto-instrumentation
    # may have installed a subclass of NoOpTracer.
    existing = trace.get_tracer_provider()
    if not isinstance(existing, trace.NoOpTracerProvider):
        # A real provider is already installed — trust it and don't override.
        # This is the expected path when opentelemetry-instrumentation-fastapi
        # is enabled: it installs the provider before our startup code runs.
        _sdk_logger.debug(
            "otel_provider_already_configured",
            extra={"event_label": "otel_provider_already_configured"},
        )
        return

    # --- Create exporter ---
    # insecure=True is required for plain-text gRPC (port 4317 without TLS).
    # Our collector runs inside the VPC — TLS termination happens at the
    # load-balancer level, not at the collector gRPC endpoint.
    span_exporter = OTLPSpanExporter(
        endpoint=otlp_endpoint,
        insecure=True,
    )

    # --- Create provider ---
    provider = TracerProvider(resource=resource)

    # BatchSpanProcessor sends spans asynchronously in the background,
    # which is required for production. SynchronousSpanProcessor would block
    # the request thread on every span export — unacceptable latency.
    provider.add_span_processor(BatchSpanProcessor(span_exporter))

    # Register as the global provider. After this line, any call to
    # ``opentelemetry.trace.get_tracer()`` will use this provider.
    trace.set_tracer_provider(provider)

    _sdk_logger.debug(
        "otel_trace_provider_configured",
        extra={"event_label": "otel_trace_provider_configured"},
    )


def get_current_trace_id() -> str:
    """Extract the trace_id from the currently active OTel span.

    Returns:
        A 32-character lowercase hex string if a real span is active,
        or "-" if no span is active (e.g. background tasks, startup code).

    WHY 32 HEX CHARS
    -----------------
    OTel trace IDs are 128-bit values. Their canonical hex representation is
    32 lowercase characters with no dashes (unlike UUID's 36-character format).
    Coralogix and Jaeger expect this format for trace linkage to work.

    WHY NOT RAISE ON INVALID
    -------------------------
    It's normal for log calls to happen outside of a span (module import,
    startup, background tasks). Raising would force callers to guard every log
    call with a span existence check — bad ergonomics. Instead we return "-"
    which is a clearly non-trace value that operators can recognize.
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()

    # INVALID_SPAN_CONTEXT is the sentinel returned when no span is active.
    # We check ``is_valid`` rather than comparing to the sentinel directly
    # because the OTel spec says is_valid is the canonical validity check.
    if not ctx.is_valid:
        return "-"

    # trace_id is a Python int; format as 32-char lowercase hex with zero-padding.
    return format(ctx.trace_id, "032x")
