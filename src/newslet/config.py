"""Environment-driven settings for newslet.

All AWS Lambda env vars are read here exactly once via :func:`settings`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Settings:
    # Anthropic
    anthropic_api_key: str
    claude_model: str

    # Resend
    resend_api_key: str
    from_email: str
    to_email: str

    # Auth / signing
    admin_token: str
    signing_key: str

    # Public base URL for rate links (https://<api-id>.execute-api.<region>.amazonaws.com)
    public_base_url: str

    # AWS / Dynamo
    aws_region: str
    table_feeds: str
    table_profile: str
    table_seen: str
    table_issues: str
    table_feedback: str


def _required(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        anthropic_api_key=_required("ANTHROPIC_API_KEY"),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
        resend_api_key=_required("RESEND_API_KEY"),
        from_email=_required("FROM_EMAIL"),
        to_email=_required("TO_EMAIL"),
        admin_token=_required("ADMIN_TOKEN"),
        signing_key=_required("SIGNING_KEY"),
        public_base_url=_required("PUBLIC_BASE_URL"),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        table_feeds=os.environ.get("TABLE_FEEDS", "newslet-feeds"),
        table_profile=os.environ.get("TABLE_PROFILE", "newslet-profile"),
        table_seen=os.environ.get("TABLE_SEEN", "newslet-seen-articles"),
        table_issues=os.environ.get("TABLE_ISSUES", "newslet-issues"),
        table_feedback=os.environ.get("TABLE_FEEDBACK", "newslet-feedback"),
    )
