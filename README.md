# neoflo-logger-sdk

Neoflo platform structured logging SDK for Python microservices.

## What it does

Every log call is serialized as a single-line JSON object and written to stdout. In parallel, if an OTEL endpoint is configured, the same telemetry is shipped to the OTEL collector via gRPC for Coralogix ingestion.

**Every log line carries:**

| Field | Source |
|---|---|
| `timestamp` | ISO 8601 UTC with milliseconds |
| `level` | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `service` | Set at `configure_logging()` |
| `environment` | Set at `configure_logging()` |
| `request_id` | Propagated from `X-Request-ID` header (or generated) |
| `task_id` | Fresh UUID4 per request |
| `trace_id` | Active OpenTelemetry span trace ID |
| `file` | Caller's filename |
| `function` | Caller's function name |
| `line` | Caller's line number |
| `label` | First argument to `logger.info()` etc. |
| `data` | Dict passed via `data=` (optional) |

Sensitive keys (`password`, `token`, `secret`, `key`, `hash`, `authorization`) are automatically replaced with `***REDACTED***`.

## Installation

```bash
pip install git+ssh://git@github.com/neofloai/neoflo-logger-sdk.git@main
```

For development (editable install):

```bash
git clone git@github.com:neofloai/neoflo-logger-sdk.git
cd neoflo-logger-sdk
pip install -e ".[dev]"
```

## Usage

### 1. Configure once at startup

```python
import os
from fastapi import FastAPI
from neoflo_logger import configure_logging, get_logger
from neoflo_logger.middleware import RequestIDMiddleware

configure_logging(
    service_name="invoice-validator-be",
    otlp_endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", ""),
    environment=os.environ.get("ENVIRONMENT", "dev"),
    debug=os.environ.get("DEBUG", "false").lower() == "true",
)

app = FastAPI()
app.add_middleware(RequestIDMiddleware)
```

### 2. Get a logger in any module

```python
from neoflo_logger import get_logger

logger = get_logger(__name__)
```

### 3. Log structured events

```python
logger.info("invoice_fetched", data={"invoice_id": "INV-002", "duration_ms": 71.8})
logger.warning("invoice_not_found", data={"invoice_id": "bad-001"})
logger.error("db_connection_failed", data={"host": "mongo", "retry": 3})
logger.debug("token_decoded", data={"user_id": "abc"})
logger.critical("service_unavailable", data={"dependency": "n8n"})

# Capture exception traceback automatically
try:
    risky_operation()
except Exception:
    logger.exception("risky_operation_failed", data={"context": "..."})
```

### Example output

```json
{
  "timestamp": "2026-06-13T15:16:11.543Z",
  "level": "INFO",
  "service": "invoice-validator-be",
  "environment": "production",
  "request_id": "abc-123",
  "task_id": "def-456",
  "trace_id": "5d2410e7773bcfccbf5262de3e1ec636",
  "file": "invoice_service.py",
  "function": "fetch_invoice",
  "line": 102,
  "label": "invoice_fetched",
  "data": {"invoice_id": "INV-002", "duration_ms": 71.8}
}
```

## Environment variables

| Variable | Used by | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `configure_logging(otlp_endpoint=...)` | `""` (OTLP disabled) |
| `ENVIRONMENT` | `configure_logging(environment=...)` | `"dev"` |
| `DEBUG` | `configure_logging(debug=...)` | `"false"` |

## Development

```bash
pip install -e ".[dev]"
pytest
```

Requirements: Python 3.11+
