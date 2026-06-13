"""
Unit tests for _formatter.py — JSON envelope structure and edge cases.
"""

from __future__ import annotations

import json
import logging

import pytest

from neoflo_logger._formatter import JsonFormatter


def _make_record(
    level: int = logging.INFO,
    msg: str = "test_event",
    event_label: str | None = None,
    event_data: dict | None = None,
    neoflo_request_id: str = "req-123",
    neoflo_task_id: str = "task-456",
    neoflo_trace_id: str = "abcdef" * 5 + "ab",
    neoflo_service: str = "test-svc",
    neoflo_environment: str = "dev",
) -> logging.LogRecord:
    """Build a LogRecord that looks as if it passed through ContextInjectingFilter."""
    record = logging.LogRecord(
        name="test.module",
        level=level,
        pathname="/app/test/invoice_service.py",
        lineno=42,
        msg=msg,
        args=(),
        exc_info=None,
    )
    record.funcName = "fetch_invoice"
    record.filename = "invoice_service.py"

    # Simulate what ContextInjectingFilter injects
    record.neoflo_request_id = neoflo_request_id  # type: ignore[attr-defined]
    record.neoflo_task_id = neoflo_task_id  # type: ignore[attr-defined]
    record.neoflo_trace_id = neoflo_trace_id  # type: ignore[attr-defined]
    record.neoflo_service = neoflo_service  # type: ignore[attr-defined]
    record.neoflo_environment = neoflo_environment  # type: ignore[attr-defined]

    # Simulate what StructuredLogger._emit() injects
    if event_label is not None:
        record.event_label = event_label  # type: ignore[attr-defined]
    if event_data is not None:
        record.event_data = event_data  # type: ignore[attr-defined]

    return record


class TestJsonEnvelope:
    """The formatter produces valid JSON with all required fields."""

    def setup_method(self):
        self.formatter = JsonFormatter()

    def test_output_is_valid_json(self):
        record = _make_record()
        output = self.formatter.format(record)
        parsed = json.loads(output)
        assert isinstance(parsed, dict)

    def test_timestamp_format(self):
        record = _make_record()
        parsed = json.loads(self.formatter.format(record))
        ts = parsed["timestamp"]
        # Must match: 2026-06-13T15:16:11.543Z
        assert ts.endswith("Z")
        assert "T" in ts
        assert len(ts) == 24  # YYYY-MM-DDTHH:MM:SS.mmmZ

    def test_level_field(self):
        record = _make_record(level=logging.WARNING)
        parsed = json.loads(self.formatter.format(record))
        assert parsed["level"] == "WARNING"

    def test_service_field(self):
        record = _make_record(neoflo_service="invoice-be")
        parsed = json.loads(self.formatter.format(record))
        assert parsed["service"] == "invoice-be"

    def test_environment_field(self):
        record = _make_record(neoflo_environment="production")
        parsed = json.loads(self.formatter.format(record))
        assert parsed["environment"] == "production"

    def test_request_id_field(self):
        record = _make_record(neoflo_request_id="my-req-id")
        parsed = json.loads(self.formatter.format(record))
        assert parsed["request_id"] == "my-req-id"

    def test_task_id_field(self):
        record = _make_record(neoflo_task_id="my-task-id")
        parsed = json.loads(self.formatter.format(record))
        assert parsed["task_id"] == "my-task-id"

    def test_trace_id_field(self):
        trace = "a" * 32
        record = _make_record(neoflo_trace_id=trace)
        parsed = json.loads(self.formatter.format(record))
        assert parsed["trace_id"] == trace

    def test_file_field(self):
        record = _make_record()
        parsed = json.loads(self.formatter.format(record))
        assert parsed["file"] == "invoice_service.py"

    def test_function_field(self):
        record = _make_record()
        parsed = json.loads(self.formatter.format(record))
        assert parsed["function"] == "fetch_invoice"

    def test_line_field(self):
        record = _make_record()
        parsed = json.loads(self.formatter.format(record))
        assert parsed["line"] == 42

    def test_label_from_event_label(self):
        record = _make_record(event_label="invoice_fetched")
        parsed = json.loads(self.formatter.format(record))
        assert parsed["label"] == "invoice_fetched"

    def test_label_falls_back_to_msg(self):
        # When event_label is not set, label should be the raw message
        record = _make_record(msg="fallback_msg")
        # Don't set event_label
        parsed = json.loads(self.formatter.format(record))
        assert parsed["label"] == "fallback_msg"

    def test_data_field_present_when_set(self):
        data = {"invoice_id": "INV-001", "amount": 500.0}
        record = _make_record(event_data=data)
        parsed = json.loads(self.formatter.format(record))
        assert parsed["data"] == data

    def test_data_field_absent_when_not_set(self):
        record = _make_record()
        parsed = json.loads(self.formatter.format(record))
        assert "data" not in parsed

    def test_all_required_fields_present(self):
        record = _make_record(event_label="test_label", event_data={"k": "v"})
        parsed = json.loads(self.formatter.format(record))
        required = {
            "timestamp", "level", "service", "environment",
            "request_id", "task_id", "trace_id",
            "file", "function", "line", "label", "data",
        }
        assert required.issubset(parsed.keys())


class TestFormatterErrorHandling:
    """Formatter must never raise — it must emit a fallback JSON line."""

    def test_fallback_on_bad_record(self):
        formatter = JsonFormatter()
        # Create a minimal record that will hit the formatter error path
        # by making one of the attribute accesses fail
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="ok", args=(), exc_info=None,
        )
        # Delete the lineno attribute to trigger an error in format
        del record.lineno
        # Should NOT raise; should return a valid JSON string
        result = formatter.format(record)
        parsed = json.loads(result)
        assert "label" in parsed  # fallback envelope has label field
