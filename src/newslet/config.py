"""Settings for newslet, read exactly once via :func:`settings`.

Secrets (Anthropic key, Resend key, admin token, signing key) come from
SSM Parameter Store SecureString parameters under ``/newslet/*`` at cold
start, with an env-var override (env wins if set) so tests and local
dev don't need AWS. Non-secret config stays in plain env vars.
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

    # Public base URL for rate links (https://<api-id>.execute-api.<region>.amazonaws.com).
    # Required for the digest Lambda (no request context); the web Lambda
    # derives this from request.base_url at runtime and leaves it empty.
    public_base_url: str

    # Name of the digest Lambda, so the web Lambda can async-invoke it for
    # the admin "send now" button. Empty on the digest Lambda itself.
    digest_function_name: str

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
def _ssm_client():
    import boto3

    return boto3.client("ssm")


def _secret(env_name: str, ssm_suffix: str) -> str:
    """Return a secret value, preferring the env var if set."""
    val = os.environ.get(env_name)
    if val:
        return val
    prefix = os.environ.get("NEWSLET_SSM_PREFIX", "/newslet")
    full = f"{prefix}/{ssm_suffix}"
    try:
        resp = _ssm_client().get_parameter(Name=full, WithDecryption=True)
    except Exception as e:
        raise RuntimeError(
            f"Secret '{env_name}' not in env and SSM lookup for '{full}' failed: {e}"
        ) from e
    return resp["Parameter"]["Value"]


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings(
        anthropic_api_key=_secret("ANTHROPIC_API_KEY", "anthropic-api-key"),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-4-7"),
        resend_api_key=_secret("RESEND_API_KEY", "resend-api-key"),
        from_email=_required("FROM_EMAIL"),
        to_email=_required("TO_EMAIL"),
        admin_token=_secret("ADMIN_TOKEN", "admin-token"),
        signing_key=_secret("SIGNING_KEY", "signing-key"),
        # Not required on the web Lambda — see Settings.public_base_url.
        public_base_url=os.environ.get("PUBLIC_BASE_URL", ""),
        digest_function_name=os.environ.get("DIGEST_FUNCTION_NAME", ""),
        aws_region=os.environ.get("AWS_REGION", "us-east-1"),
        table_feeds=os.environ.get("TABLE_FEEDS", "newslet-feeds"),
        table_profile=os.environ.get("TABLE_PROFILE", "newslet-profile"),
        table_seen=os.environ.get("TABLE_SEEN", "newslet-seen-articles"),
        table_issues=os.environ.get("TABLE_ISSUES", "newslet-issues"),
        table_feedback=os.environ.get("TABLE_FEEDBACK", "newslet-feedback"),
    )
