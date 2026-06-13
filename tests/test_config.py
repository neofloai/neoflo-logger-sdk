"""
Unit tests for _config.py — LoggerConfig dataclass and singleton management.
"""

from __future__ import annotations

import logging

import pytest

from neoflo_logger._config import (
    LoggerConfig,
    _reset_config,
    get_config,
    is_configured,
    set_config,
)


@pytest.fixture(autouse=True)
def clean_config():
    _reset_config()
    yield
    _reset_config()


class TestLoggerConfig:
    """LoggerConfig is a frozen dataclass — test immutability and field presence."""

    def test_fields_accessible(self):
        cfg = LoggerConfig(
            service_name="test-svc",
            otlp_endpoint="http://otel:4317",
            environment="dev",
            debug=True,
            min_level=logging.DEBUG,
            enable_otlp=True,
        )
        assert cfg.service_name == "test-svc"
        assert cfg.environment == "dev"
        assert cfg.debug is True
        assert cfg.min_level == logging.DEBUG
        assert cfg.enable_otlp is True

    def test_frozen_prevents_mutation(self):
        cfg = LoggerConfig(
            service_name="svc",
            otlp_endpoint="",
            environment="prod",
            debug=False,
            min_level=logging.INFO,
            enable_otlp=False,
        )
        with pytest.raises(Exception):  # FrozenInstanceError is a dataclasses error
            cfg.service_name = "hacked"  # type: ignore[misc]


class TestSingleton:
    """get_config / set_config / is_configured behave as a proper singleton."""

    def test_get_config_raises_before_set(self):
        with pytest.raises(RuntimeError, match="not been configured"):
            get_config()

    def test_is_configured_false_before_set(self):
        assert is_configured() is False

    def test_set_and_get_config(self):
        cfg = LoggerConfig(
            service_name="svc",
            otlp_endpoint="",
            environment="dev",
            debug=False,
            min_level=logging.INFO,
            enable_otlp=False,
        )
        set_config(cfg)
        assert is_configured() is True
        assert get_config() is cfg

    def test_reset_config_clears_singleton(self):
        cfg = LoggerConfig(
            service_name="svc",
            otlp_endpoint="",
            environment="dev",
            debug=False,
            min_level=logging.INFO,
            enable_otlp=False,
        )
        set_config(cfg)
        _reset_config()
        assert is_configured() is False
