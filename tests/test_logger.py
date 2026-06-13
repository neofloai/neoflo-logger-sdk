"""
Unit tests for _logger.py — StructuredLogger sanitization, level dispatch, and emission.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from neoflo_logger._logger import StructuredLogger, _REDACT_SUBSTRINGS, _REDACTED_VALUE


class TestSanitize:
    """_sanitize() never mutates the input dict and redacts the right keys."""

    def setup_method(self):
        self.logger = StructuredLogger("test")

    def test_clean_data_unchanged(self):
        data = {"invoice_id": "INV-001", "amount": 500.0}
        result = self.logger._sanitize(data)
        assert result == data

    def test_does_not_mutate_original(self):
        original = {"password": "hunter2", "user": "alice"}
        result = self.logger._sanitize(original)
        # Original must be untouched
        assert original["password"] == "hunter2"
        # Sanitized result must have REDACTED
        assert result["password"] == _REDACTED_VALUE

    def test_redacts_password(self):
        result = self.logger._sanitize({"password": "s3cr3t"})
        assert result["password"] == _REDACTED_VALUE

    def test_redacts_token(self):
        result = self.logger._sanitize({"access_token": "eyJhb..."})
        assert result["access_token"] == _REDACTED_VALUE

    def test_redacts_secret(self):
        result = self.logger._sanitize({"client_secret": "xyz"})
        assert result["client_secret"] == _REDACTED_VALUE

    def test_redacts_api_key(self):
        result = self.logger._sanitize({"api_key": "abc123"})
        assert result["api_key"] == _REDACTED_VALUE

    def test_redacts_hash(self):
        result = self.logger._sanitize({"password_hash": "bcrypt..."})
        assert result["password_hash"] == _REDACTED_VALUE

    def test_redacts_authorization(self):
        result = self.logger._sanitize({"authorization": "Bearer ..."})
        assert result["authorization"] == _REDACTED_VALUE

    def test_case_insensitive_redaction(self):
        # Keys in camelCase or UPPER should still be redacted
        result = self.logger._sanitize({"apiKey": "abc", "SECRET": "xyz"})
        assert result["apiKey"] == _REDACTED_VALUE
        assert result["SECRET"] == _REDACTED_VALUE

    def test_non_sensitive_keys_preserved(self):
        data = {"invoice_id": "INV-001", "vendor_name": "Acme"}
        result = self.logger._sanitize(data)
        assert result["invoice_id"] == "INV-001"
        assert result["vendor_name"] == "Acme"

    def test_empty_dict(self):
        assert self.logger._sanitize({}) == {}


class TestLevelMethods:
    """Each level method only emits when the underlying logger is enabled."""

    def setup_method(self):
        self.slogger = StructuredLogger("test.levels")
        # Patch the underlying stdlib logger to capture calls
        self.mock_logger = MagicMock()
        self.slogger._logger = self.mock_logger

    def test_debug_not_emitted_when_disabled(self):
        self.mock_logger.isEnabledFor.return_value = False
        self.slogger.debug("noop_event")
        self.mock_logger.log.assert_not_called()

    def test_debug_emitted_when_enabled(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.debug("debug_event", data={"x": 1})
        self.mock_logger.log.assert_called_once()

    def test_info_calls_log_with_correct_level(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.info("info_event")
        call_args = self.mock_logger.log.call_args
        assert call_args[0][0] == logging.INFO

    def test_warning_calls_log_with_correct_level(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.warning("warn_event")
        call_args = self.mock_logger.log.call_args
        assert call_args[0][0] == logging.WARNING

    def test_warn_is_alias_for_warning(self):
        # warn and warning should both work
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.warn("alias_event")
        self.mock_logger.log.assert_called_once()

    def test_error_calls_log_with_correct_level(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.error("error_event")
        call_args = self.mock_logger.log.call_args
        assert call_args[0][0] == logging.ERROR

    def test_error_passes_exc_info(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.error("error_event", exc_info=True)
        call_kwargs = self.mock_logger.log.call_args[1]
        assert call_kwargs["exc_info"] is True

    def test_critical_always_emits(self):
        # CRITICAL bypasses isEnabledFor — it must always fire
        self.mock_logger.isEnabledFor.return_value = False
        self.slogger.critical("fatal_event")
        self.mock_logger.log.assert_called_once()
        call_args = self.mock_logger.log.call_args
        assert call_args[0][0] == logging.CRITICAL

    def test_exception_sets_exc_info_true(self):
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger.exception("exc_event", data={"detail": "boom"})
        call_kwargs = self.mock_logger.log.call_args[1]
        assert call_kwargs["exc_info"] is True


class TestEmitExtra:
    """_emit() places the right keys in ``extra``."""

    def setup_method(self):
        self.slogger = StructuredLogger("test.extra")
        self.mock_logger = MagicMock()
        self.mock_logger.isEnabledFor.return_value = True
        self.slogger._logger = self.mock_logger

    def test_event_label_set(self):
        self.slogger.info("my_event")
        extra = self.mock_logger.log.call_args[1]["extra"]
        assert extra["event_label"] == "my_event"

    def test_event_data_not_set_when_no_data(self):
        self.slogger.info("my_event")
        extra = self.mock_logger.log.call_args[1]["extra"]
        assert "event_data" not in extra

    def test_event_data_set_when_data_provided(self):
        self.slogger.info("my_event", data={"k": "v"})
        extra = self.mock_logger.log.call_args[1]["extra"]
        assert extra["event_data"] == {"k": "v"}

    def test_sensitive_data_redacted_in_extra(self):
        self.slogger.info("my_event", data={"password": "secret123", "user": "alice"})
        extra = self.mock_logger.log.call_args[1]["extra"]
        assert extra["event_data"]["password"] == _REDACTED_VALUE
        assert extra["event_data"]["user"] == "alice"

    def test_stacklevel_is_3(self):
        """stacklevel=3 ensures log record points to the caller's frame."""
        self.slogger.info("event")
        call_kwargs = self.mock_logger.log.call_args[1]
        assert call_kwargs["stacklevel"] == 3
