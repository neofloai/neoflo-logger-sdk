"""
Pytest configuration and shared fixtures for the neoflo_logger test suite.

Every test that exercises configure_logging() must reset the global config
singleton between test cases. The ``reset_logger_config`` fixture handles this
automatically when requested.

All fixtures that need a clean logger state should use ``reset_logger_config``
as a dependency.
"""

from __future__ import annotations

import logging

import pytest

from neoflo_logger._config import _reset_config
from neoflo_logger._context import request_id_var, task_id_var, trace_id_var


@pytest.fixture(autouse=False)
def reset_logger_config():
    """Reset the SDK config singleton before and after every test.

    Mark individual tests or entire test classes with:
        @pytest.mark.usefixtures("reset_logger_config")

    Or request it explicitly in test parameters.

    We also clear the root logger handlers to prevent double-logging in tests
    that call configure_logging() — without this, each test call appends a new
    StreamHandler to the root logger, producing duplicated output.
    """
    _reset_config()
    root = logging.getLogger()
    root.handlers.clear()
    yield
    _reset_config()
    root.handlers.clear()


@pytest.fixture(autouse=False)
def reset_context():
    """Reset all ContextVars to their defaults between tests.

    ContextVars maintain state within an asyncio Task. In tests that run
    synchronously (most unit tests), the vars persist across test functions
    unless explicitly reset. This fixture ensures isolation.
    """
    # Store previous values (should be defaults, but be defensive)
    rid_token = request_id_var.set("-")
    tid_token = task_id_var.set("-")
    trid_token = trace_id_var.set("-")
    yield
    # Restore — using reset() is semantically correct and avoids leaving
    # the ContextVar in a set state that bleeds into the next test.
    request_id_var.reset(rid_token)
    task_id_var.reset(tid_token)
    trace_id_var.reset(trid_token)
