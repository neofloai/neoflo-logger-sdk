"""
Integration tests for the public configure_logging() / get_logger() API.

These tests actually call configure_logging() and verify the root logger is
wired correctly. They use caplog / capsys to capture log output.
"""

from __future__ import annotations

import json
import logging

import pytest

from neoflo_logger import configure_logging, get_logger
from neoflo_logger._config import _reset_config, get_config


@pytest.fixture(autouse=True)
def clean():
    _reset_config()
    root = logging.getLogger()
    root.handlers.clear()
    yield
    _reset_config()
    root.handlers.clear()


class TestConfigureLogging:
    def test_raises_on_empty_service_name(self):
        with pytest.raises(ValueError, match="service_name must be"):
            configure_logging(
                service_name="",
                otlp_endpoint="",
                environment="dev",
            )

    def test_raises_on_whitespace_service_name(self):
        with pytest.raises(ValueError, match="service_name must be"):
            configure_logging(
                service_name="   ",
                otlp_endpoint="",
                environment="dev",
            )

    def test_config_stored_correctly(self):
        configure_logging(
            service_name="test-svc",
            otlp_endpoint="",
            environment="staging",
            debug=False,
        )
        cfg = get_config()
        assert cfg.service_name == "test-svc"
        assert cfg.environment == "staging"
        assert cfg.debug is False
        assert cfg.enable_otlp is False

    def test_debug_true_sets_debug_level(self):
        configure_logging(
            service_name="test-svc",
            otlp_endpoint="",
            environment="dev",
            debug=True,
        )
        assert get_config().min_level == logging.DEBUG

    def test_debug_false_sets_info_level(self):
        configure_logging(
            service_name="test-svc",
            otlp_endpoint="",
            environment="production",
            debug=False,
        )
        assert get_config().min_level == logging.INFO

    def test_otlp_enabled_when_endpoint_set(self):
        configure_logging(
            service_name="test-svc",
            otlp_endpoint="http://otel:4317",
            environment="dev",
        )
        # OTLP setup may fail (no collector running) but the config should be set
        assert get_config().enable_otlp is True

    def test_second_call_is_noop(self):
        configure_logging(
            service_name="first-svc",
            otlp_endpoint="",
            environment="dev",
        )
        # Second call should not override
        configure_logging(
            service_name="second-svc",
            otlp_endpoint="",
            environment="prod",
        )
        assert get_config().service_name == "first-svc"

    def test_handler_attached_to_root_logger(self):
        configure_logging(
            service_name="test-svc",
            otlp_endpoint="",
            environment="dev",
        )
        root = logging.getLogger()
        assert len(root.handlers) == 1

    def test_service_name_stripped(self):
        configure_logging(
            service_name="  my-service  ",
            otlp_endpoint="",
            environment="dev",
        )
        assert get_config().service_name == "my-service"


class TestGetLogger:
    def test_returns_structured_logger(self):
        from neoflo_logger._logger import StructuredLogger

        logger = get_logger(__name__)
        assert isinstance(logger, StructuredLogger)

    def test_same_name_same_underlying_logger(self):
        logger1 = get_logger("mymodule")
        logger2 = get_logger("mymodule")
        assert logger1._logger is logger2._logger


class TestJsonOutputFormat:
    """End-to-end: configure → log → capture stdout → parse JSON."""

    def test_info_produces_json_output(self, capsys):
        configure_logging(
            service_name="e2e-svc",
            otlp_endpoint="",
            environment="test",
            debug=True,
        )
        logger = get_logger("e2e.test")
        logger.info("order_created", data={"order_id": "ORD-001"})

        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().splitlines() if l.strip()]
        assert len(lines) >= 1

        record = json.loads(lines[-1])
        assert record["label"] == "order_created"
        assert record["level"] == "INFO"
        assert record["service"] == "e2e-svc"
        assert record["environment"] == "test"
        assert record["data"]["order_id"] == "ORD-001"

    def test_sensitive_data_redacted_in_output(self, capsys):
        configure_logging(
            service_name="e2e-svc",
            otlp_endpoint="",
            environment="test",
            debug=True,
        )
        logger = get_logger("e2e.secret")
        logger.info("user_login", data={"username": "alice", "password": "hunter2"})

        captured = capsys.readouterr()
        lines = [l for l in captured.out.strip().splitlines() if l.strip()]
        record = json.loads(lines[-1])
        assert record["data"]["password"] == "***REDACTED***"
        assert record["data"]["username"] == "alice"

    def test_debug_suppressed_at_info_level(self, capsys):
        configure_logging(
            service_name="e2e-svc",
            otlp_endpoint="",
            environment="test",
            debug=False,  # INFO level
        )
        logger = get_logger("e2e.debug")
        logger.debug("this_should_not_appear")

        captured = capsys.readouterr()
        # No debug output expected
        output_text = captured.out.strip()
        if output_text:
            for line in output_text.splitlines():
                record = json.loads(line)
                assert record.get("level") != "DEBUG"
