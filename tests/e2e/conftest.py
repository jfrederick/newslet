"""Fixtures for the browser end-to-end suite.

These tests drive the *real* FastAPI app in a browser, but stay fully offline:
``moto`` mocks DynamoDB in-process and ``uvicorn`` serves the ASGI app on a
local port in a background thread, so the same process's moto patches apply to
the server's boto3 calls. No network, no real AWS — same invariant as the rest
of the suite (see AGENTS.md "Testing patterns").

Anthropic is never reached: the flows exercised here (login, browse the
homepage, vote, admin CRUD) read stored DynamoDB rows and never call the
ranking/summarize/web-search paths.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import boto3
import moto
import pytest
import uvicorn


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this package ``e2e`` so the default run skips them.

    A module-level ``pytestmark`` in a conftest does not propagate to sibling
    test files, so apply the marker by location instead.
    """
    for item in items:
        if "tests/e2e/" in item.nodeid or "tests\\e2e\\" in item.nodeid:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "supersecret")
    monkeypatch.setenv("SIGNING_KEY", "signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("MAIL_DOMAIN", "inbox.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from newslet.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


def _create_tables(ddb) -> None:
    ddb.create_table(
        TableName="newslet-feeds",
        KeySchema=[{"AttributeName": "url", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "url", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-profile",
        KeySchema=[{"AttributeName": "id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-seen-articles",
        KeySchema=[{"AttributeName": "url_hash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "url_hash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-issues",
        KeySchema=[{"AttributeName": "date", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "date", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-feedback",
        KeySchema=[
            {"AttributeName": "article_url", "KeyType": "HASH"},
            {"AttributeName": "issue_date", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "article_url", "AttributeType": "S"},
            {"AttributeName": "issue_date", "AttributeType": "S"},
            {"AttributeName": "bucket", "AttributeType": "S"},
            {"AttributeName": "ts", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "feedback-by-ts",
                "KeySchema": [
                    {"AttributeName": "bucket", "KeyType": "HASH"},
                    {"AttributeName": "ts", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-subscriptions",
        KeySchema=[{"AttributeName": "address", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "address", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    ddb.create_table(
        TableName="newslet-inbox",
        KeySchema=[{"AttributeName": "url_hash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "url_hash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def aws(env: None) -> Iterator[None]:
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_tables(ddb)
        yield


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


@pytest.fixture
def live_server(aws: None) -> Iterator[str]:
    """Serve the ASGI app on a local port and yield its base URL.

    Runs in a daemon thread of *this* process so moto's in-process boto3
    patches (active via the ``aws`` fixture) intercept the server's DynamoDB
    calls. ``db._resource()`` builds a fresh resource per call, so every
    request binds to the mock rather than a stale real-AWS client.
    """
    from newslet.handlers.web import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start within 10s")
        time.sleep(0.05)

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
