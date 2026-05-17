"""Integration tests for the FastAPI web app, backed by moto DynamoDB."""

from __future__ import annotations

from urllib.parse import quote

import boto3
import moto
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("FROM_EMAIL", "from@example.com")
    monkeypatch.setenv("TO_EMAIL", "to@example.com")
    monkeypatch.setenv("ADMIN_TOKEN", "supersecret")
    monkeypatch.setenv("SIGNING_KEY", "signing-key")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    from newslet.config import settings

    settings.cache_clear()
    yield
    settings.cache_clear()


@pytest.fixture
def aws(env):
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
                {"AttributeName": "ts", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "article_url", "AttributeType": "S"},
                {"AttributeName": "ts", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield


@pytest.fixture
def client(aws):
    from newslet.handlers.web import app

    # follow_redirects=False so we can inspect the 303s.
    return TestClient(app, follow_redirects=False)


def test_unauthenticated_root_redirects_to_login(client):
    r = client.get("/")
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_login_bad_token(client):
    r = client.post("/login", data={"token": "wrong"})
    assert r.status_code == 200
    assert "Invalid token" in r.text


def test_login_good_token_sets_cookie(client):
    r = client.post("/login", data={"token": "supersecret"})
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    assert "admin_token=supersecret" in r.headers["set-cookie"]


def test_add_and_delete_feed_roundtrip(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post(
        "/api/feeds",
        data={"url": "https://example.com/rss", "title": "Example"},
    )
    assert r.status_code == 303

    r = client.get("/")
    assert r.status_code == 200
    # Look for the delete form which only renders for actual feed rows
    assert 'value="https://example.com/rss"' in r.text

    r = client.post("/api/feeds/delete", data={"url": "https://example.com/rss"})
    assert r.status_code == 303
    r = client.get("/")
    assert 'value="https://example.com/rss"' not in r.text


def test_profile_save(client):
    client.cookies.set("admin_token", "supersecret")
    r = client.post("/api/profile", data={"markdown": "I like LLMs and Postgres."})
    assert r.status_code == 303
    r = client.get("/")
    assert "I like LLMs and Postgres." in r.text


def test_rate_rejects_unsigned_token(client):
    url = "https://example.com/article"
    r = client.get(
        "/rate",
        params={"a": quote(url, safe=""), "d": "2026-05-17", "v": "up", "t": "garbage"},
    )
    assert r.status_code == 403


def test_rate_accepts_signed_token(client):
    from newslet import tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": quote(url, safe=""), "d": "2026-05-17", "v": "up", "t": token},
    )
    assert r.status_code == 200
    assert "thanks" in r.text.lower()


def test_rate_rejects_bad_rating(client):
    from newslet import tokens

    url = "https://example.com/article"
    token = tokens.sign(url, "2026-05-17")
    r = client.get(
        "/rate",
        params={"a": quote(url, safe=""), "d": "2026-05-17", "v": "sideways", "t": token},
    )
    assert r.status_code == 400
