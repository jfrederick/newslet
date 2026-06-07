"""Smoke-level browser tests for the admin-gated web surfaces.

Kept deliberately small: enough to prove the Playwright + uvicorn + moto
harness works end to end against real routes. Grow this file with richer
interaction tests (voting, subject search, Refresh) as needed.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def _login(page: Page, base_url: str) -> None:
    page.goto(f"{base_url}/")
    # Unauthenticated visits redirect to the login form.
    expect(page).to_have_url(f"{base_url}/login")
    page.fill("input[name=token]", "supersecret")
    page.click("button[type=submit]")


def test_unauthenticated_root_redirects_to_login(live_server: str, page: Page) -> None:
    page.goto(f"{live_server}/")
    expect(page).to_have_url(f"{live_server}/login")


def test_login_with_bad_token_shows_error(live_server: str, page: Page) -> None:
    page.goto(f"{live_server}/login")
    page.fill("input[name=token]", "wrong")
    page.click("button[type=submit]")
    expect(page.locator("body")).to_contain_text("Invalid token")


def test_login_then_homepage_renders(live_server: str, page: Page) -> None:
    _login(page, live_server)
    expect(page).to_have_url(f"{live_server}/")
    expect(page).to_have_title("newslet")


def test_admin_feed_roundtrip(live_server: str, page: Page) -> None:
    _login(page, live_server)
    page.goto(f"{live_server}/admin")
    expect(page).to_have_title("newslet · admin")

    # Add a feed through the real form and confirm it shows up.
    page.fill("form[action='/api/feeds'] input[name=url]", "https://example.com/rss")
    page.click("form[action='/api/feeds'] button[type=submit]")
    expect(page.locator("input[value='https://example.com/rss']")).to_have_count(1)

    # Delete it again (the delete form confirms via window.confirm).
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("form[action='/api/feeds/delete'] button[type=submit]")
    expect(page.locator("input[value='https://example.com/rss']")).to_have_count(0)
