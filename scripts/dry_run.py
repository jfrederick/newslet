"""Local dry-run: render a sample email to `out/email.html`.

Uses fake articles (no network, no Anthropic, no DynamoDB) so you can
eyeball the email layout before deploying.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Make the venv-installed package importable when run as `python scripts/dry_run.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Populate dummy env so settings() succeeds
os.environ.setdefault("ANTHROPIC_API_KEY", "dry-run")
os.environ.setdefault("RESEND_API_KEY", "dry-run")
os.environ.setdefault("FROM_EMAIL", "newslet@example.com")
os.environ.setdefault("TO_EMAIL", "you@example.com")
os.environ.setdefault("ADMIN_TOKEN", "dry-run")
os.environ.setdefault("SIGNING_KEY", "dry-run-signing-key")
os.environ.setdefault("PUBLIC_BASE_URL", "https://api.example.com")

from newslet import email_render  # noqa: E402
from newslet.contracts import Issue, Pick  # noqa: E402

FIXTURE_PICKS = [
    Pick(
        url="https://www.lesswrong.com/posts/example/a-quiet-revolution-in-protein-design",
        title="A quiet revolution in protein design",
        blurb="New diffusion models can hallucinate proteins that bind to arbitrary targets, with hit rates that would have been unthinkable two years ago.",
        source="LessWrong",
        score=0.94,
    ),
    Pick(
        url="https://stratechery.com/2026/example/the-bundling-of-everything",
        title="The bundling of everything",
        blurb="Ben Thompson on why the long tail of subscriptions is collapsing back into a handful of mega-bundles.",
        source="Stratechery",
        score=0.88,
    ),
    Pick(
        url="https://www.nature.com/articles/example-room-temperature-superconductor",
        title="Room-temperature superconductor synthesised in ambient pressure",
        blurb="Independent replication of the LK-99 follow-up confirms zero resistance at 295K.",
        source="Nature",
        score=0.82,
    ),
    Pick(
        url="https://www.theverge.com/example/apple-vision-pro-2-leaks",
        title="Vision Pro 2 leaks point to half the weight and a third the price",
        blurb="If accurate, this is the price point that finally moves headsets out of the enthusiast tier.",
        source="The Verge",
        score=0.71,
    ),
    Pick(
        url="https://example.com/blog/postgres-19-async-io",
        title="Postgres 19 lands async I/O",
        blurb="Real-world benchmarks show 2-3x improvement on read-heavy OLTP workloads.",
        source="Crunchy Data Blog",
        score=0.64,
    ),
]


def main() -> int:
    issue = Issue(
        date=datetime.now(UTC).strftime("%Y-%m-%d"),
        picks=FIXTURE_PICKS,
        created_at=datetime.now(UTC),
    )
    subject, html = email_render.render_email(issue, "https://api.example.com")
    out = Path(__file__).resolve().parent.parent / "out" / "email.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    print(f"subject: {subject}")
    print(f"wrote {out} ({len(html)} bytes, {len(issue.picks)} picks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
