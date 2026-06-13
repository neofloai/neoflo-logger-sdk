"""
Integration tests for RequestIDMiddleware.

We use Starlette's TestClient (sync) and httpx.AsyncClient (async) to exercise
the full middleware chain without requiring a running server.
"""

from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from neoflo_logger._config import _reset_config
from neoflo_logger._context import request_id_var, task_id_var
from neoflo_logger.middleware import RequestIDMiddleware, _REQUEST_ID_RE


# ---------------------------------------------------------------------------
# Minimal Starlette app for testing
# ---------------------------------------------------------------------------


async def echo_ids(request: Request) -> JSONResponse:
    """Handler that echoes the current ContextVar values so tests can verify them."""
    return JSONResponse(
        {
            "request_id": request_id_var.get(),
            "task_id": task_id_var.get(),
        }
    )


async def raise_handler(request: Request) -> JSONResponse:
    raise RuntimeError("intentional error")


def _make_app(log_requests: bool = False) -> Starlette:
    return Starlette(
        routes=[
            Route("/echo", echo_ids),
            Route("/error", raise_handler),
        ],
        middleware=[
            # Starlette middleware list takes (cls, **kwargs) tuples
            # We pass log_requests=False to suppress middleware log output in tests
        ],
    )


def _make_client(log_requests: bool = False) -> TestClient:
    app = Starlette(
        routes=[
            Route("/echo", echo_ids),
            Route("/error", raise_handler),
        ]
    )
    app.add_middleware(RequestIDMiddleware, log_requests=log_requests)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean():
    _reset_config()
    yield
    _reset_config()


# ---------------------------------------------------------------------------
# Request ID validation regex
# ---------------------------------------------------------------------------


class TestRequestIDRegex:
    def test_valid_uuid4(self):
        assert _REQUEST_ID_RE.match("550e8400-e29b-41d4-a716-446655440000")

    def test_valid_short_id(self):
        assert _REQUEST_ID_RE.match("abc-123")

    def test_valid_64_chars(self):
        assert _REQUEST_ID_RE.match("a" * 64)

    def test_rejects_too_long(self):
        assert not _REQUEST_ID_RE.match("a" * 65)

    def test_rejects_empty(self):
        assert not _REQUEST_ID_RE.match("")

    def test_rejects_slash(self):
        assert not _REQUEST_ID_RE.match("abc/def")

    def test_rejects_space(self):
        assert not _REQUEST_ID_RE.match("abc def")

    def test_rejects_sql_injection(self):
        assert not _REQUEST_ID_RE.match("1; DROP TABLE users;--")


# ---------------------------------------------------------------------------
# Middleware behaviour tests
# ---------------------------------------------------------------------------

_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


class TestMiddlewarePropagation:
    def setup_method(self):
        self.client = _make_client()

    def test_valid_request_id_propagated(self):
        resp = self.client.get("/echo", headers={"X-Request-ID": "my-req-001"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["request_id"] == "my-req-001"

    def test_missing_request_id_generates_uuid4(self):
        resp = self.client.get("/echo")
        body = resp.json()
        assert _UUID4_RE.match(body["request_id"]), f"Not a UUID4: {body['request_id']}"

    def test_invalid_request_id_generates_uuid4(self):
        resp = self.client.get("/echo", headers={"X-Request-ID": "bad id with spaces"})
        body = resp.json()
        # Should be a freshly generated UUID4, not the invalid input
        assert _UUID4_RE.match(body["request_id"])

    def test_task_id_always_uuid4(self):
        resp = self.client.get("/echo", headers={"X-Request-ID": "any-req"})
        body = resp.json()
        assert _UUID4_RE.match(body["task_id"]), f"Not a UUID4: {body['task_id']}"

    def test_task_id_differs_between_requests(self):
        r1 = self.client.get("/echo").json()
        r2 = self.client.get("/echo").json()
        assert r1["task_id"] != r2["task_id"]

    def test_response_header_x_request_id_set(self):
        resp = self.client.get("/echo", headers={"X-Request-ID": "hdr-test"})
        assert resp.headers["X-Request-ID"] == "hdr-test"

    def test_response_header_x_task_id_set(self):
        resp = self.client.get("/echo")
        assert "X-Task-ID" in resp.headers
        assert _UUID4_RE.match(resp.headers["X-Task-ID"])

    def test_request_id_in_response_matches_body(self):
        resp = self.client.get("/echo", headers={"X-Request-ID": "match-test"})
        body = resp.json()
        assert resp.headers["X-Request-ID"] == body["request_id"]


class TestMiddlewareErrorHandling:
    def test_exception_in_handler_does_not_crash_middleware(self):
        client = _make_client()
        # raise_server_exceptions=False means TestClient returns 500 instead of raising
        resp = client.get("/error")
        # Middleware should still set response headers on error paths (if possible)
        # The important thing is the server returns a response, not that middleware died
        assert resp.status_code == 500
