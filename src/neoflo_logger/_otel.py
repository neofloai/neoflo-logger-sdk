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
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_sdk_logger = logging.getLogger(__name__)


class _DictAttributeSerializer(logging.Filter):
    """Serialize dict/list log record attributes to JSON strings.

    OTel log attributes only accept primitive types. Any extra field set on a
    LogRecord as a dict or list (e.g. event_data) must be converted to a JSON
    string before the LoggingHandler tries to forward it as an OTel attribute.
    """

    import json as _json

    def filter(self, record: logging.LogRecord) -> bool:
        import json
        for key, val in vars(record).items():
            if isinstance(val, (dict, list)):
                setattr(record, key, json.dumps(val, default=str))
        return True


def setup_otel(
    service_name: str,
    otlp_endpoint: str,
    environment: str,
) -> None:
    """Bootstrap the global OpenTelemetry Trace and Log providers.

    This function is idempotent with respect to the global trace provider — if
    a provider is already registered (e.g. by opentelemetry-instrumentation-
    fastapi auto-instrumentation), we do NOT replace it. We only set up a
    new trace provider if the current global is the no-op default.

    The log provider is always set up when this function is called, because
    Python's logging.LoggingHandler bridges stdlib log records into the OTel
    log pipeline, which is what ships logs to the collector and Coralogix.

    Args:
        service_name: Appears as ``service.name`` in every span and log record.
        otlp_endpoint: gRPC endpoint, e.g. "http://otel-collector:4317".
        environment: Appears as ``deployment.environment`` in every span/log.
                     Enables per-environment filtering in Coralogix.
    """
    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": environment,
        }
    )

    # --- Trace pipeline ---
    existing = trace.get_tracer_provider()
    if not isinstance(existing, trace.NoOpTracerProvider):
        _sdk_logger.debug(
            "otel_provider_already_configured",
            extra={"event_label": "otel_provider_already_configured"},
        )
    else:
        span_exporter = OTLPSpanExporter(
            endpoint=otlp_endpoint,
        )
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(provider)

        _sdk_logger.debug(
            "otel_trace_provider_configured",
            extra={"event_label": "otel_trace_provider_configured"},
        )

    # --- Log pipeline ---
    # OTLPLogExporter ships log records to the collector over gRPC.
    # BatchLogRecordProcessor buffers and sends them asynchronously so log
    # calls never block the request thread.
    log_exporter = OTLPLogExporter(
        endpoint=otlp_endpoint,
    )
    log_provider = LoggerProvider(resource=resource)
    log_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    set_logger_provider(log_provider)

    # LoggingHandler bridges Python's stdlib logging module into the OTel log
    # pipeline. Any log record that reaches the root logger is forwarded to
    # log_provider, which batches and ships it to the collector.
    # level=logging.NOTSET means all levels pass through — the root logger's
    # own level filter is the single source of truth for what gets logged.
    otel_log_handler = LoggingHandler(
        level=logging.NOTSET,
        logger_provider=log_provider,
    )
    # OTel attributes only support primitives (str, int, float, bool).
    # Serialize any dict/list extras (e.g. event_data) to JSON strings so
    # the handler doesn't drop them with an "Invalid type" error.
    otel_log_handler.addFilter(_DictAttributeSerializer())
    logging.getLogger().addHandler(otel_log_handler)

    _sdk_logger.debug(
        "otel_log_provider_configured",
        extra={"event_label": "otel_log_provider_configured"},
    )


def get_current_trace_id() -> str:
    """Extract the trace_id from the currently active OTel span.

    Returns:
        A 32-character lowercase hex string if a real span is active,
        or "-" if no span is active (e.g. background tasks, startup code).
    """
    span = trace.get_current_span()
    ctx = span.get_span_context()

    if not ctx.is_valid:
        return "-"

    return format(ctx.trace_id, "032x")
