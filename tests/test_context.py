"""
Unit tests for _context.py — ContextVar definitions and typed accessors.
"""

from __future__ import annotations

import pytest

from neoflo_logger._context import (
    RequestContext,
    get_context,
    request_id_var,
    set_request_id,
    set_task_id,
    set_trace_id,
    task_id_var,
    trace_id_var,
)


@pytest.fixture(autouse=True)
def reset_context_vars():
    """Reset ContextVars to defaults before each test."""
    t1 = request_id_var.set("-")
    t2 = task_id_var.set("-")
    t3 = trace_id_var.set("-")
    yield
    request_id_var.reset(t1)
    task_id_var.reset(t2)
    trace_id_var.reset(t3)


class TestDefaults:
    def test_default_request_id(self):
        assert request_id_var.get() == "-"

    def test_default_task_id(self):
        assert task_id_var.get() == "-"

    def test_default_trace_id(self):
        assert trace_id_var.get() == "-"


class TestGetContext:
    def test_returns_request_context_namedtuple(self):
        ctx = get_context()
        assert isinstance(ctx, RequestContext)

    def test_defaults(self):
        ctx = get_context()
        assert ctx.request_id == "-"
        assert ctx.task_id == "-"
        assert ctx.trace_id == "-"

    def test_reflects_set_values(self):
        set_request_id("req-abc")
        set_task_id("task-xyz")
        set_trace_id("trace-111")

        ctx = get_context()
        assert ctx.request_id == "req-abc"
        assert ctx.task_id == "task-xyz"
        assert ctx.trace_id == "trace-111"


class TestSetters:
    def test_set_request_id_returns_token(self):
        token = set_request_id("my-request")
        assert request_id_var.get() == "my-request"
        # Reset using token restores previous value
        request_id_var.reset(token)
        assert request_id_var.get() == "-"

    def test_set_task_id_returns_token(self):
        token = set_task_id("my-task")
        assert task_id_var.get() == "my-task"
        task_id_var.reset(token)
        assert task_id_var.get() == "-"

    def test_set_trace_id_returns_token(self):
        token = set_trace_id("deadbeef" * 4)
        assert trace_id_var.get() == "deadbeef" * 4
        trace_id_var.reset(token)
        assert trace_id_var.get() == "-"
