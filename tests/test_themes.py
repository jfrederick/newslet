"""Tests for :mod:`newslet.themes`."""

from __future__ import annotations

import re
from dataclasses import fields

from newslet import themes
from newslet.themes import Palette

_HEX = re.compile(r"^#[0-9a-f]{6}$")


def test_default_is_classic() -> None:
    assert themes.DEFAULT_THEME == "classic"
    assert themes.DEFAULT_THEME in themes.THEMES


def test_get_is_lenient() -> None:
    classic = themes.THEMES["classic"]
    assert themes.get(None) is classic
    assert themes.get("") is classic
    assert themes.get("no-such-theme") is classic
    assert themes.get("phosphor") is themes.THEMES["phosphor"]


def test_expected_theme_set() -> None:
    assert set(themes.THEMES) == {
        "classic", "phosphor", "amber", "paper", "dos", "mono",
    }
    assert [t.key for t in themes.list_themes()] == list(themes.THEMES)


def test_every_palette_color_is_well_formed() -> None:
    for theme in themes.THEMES.values():
        palettes = [theme.palette] + (
            [theme.dark_palette] if theme.dark_palette else []
        )
        for palette in palettes:
            for f in fields(Palette):
                value = getattr(palette, f.name)
                assert _HEX.match(value), f"{theme.key}.{f.name} = {value!r}"


def test_keys_match_mapping() -> None:
    for key, theme in themes.THEMES.items():
        assert theme.key == key
        assert theme.label


def test_css_emits_all_variables() -> None:
    css = themes.css(themes.THEMES["phosphor"])
    for f in fields(Palette):
        assert f"--{f.name.replace('_', '-')}:" in css
    for var in ("--font-body:", "--font-brand:", "--font-mono:",
                "--radius:", "--radius-sm:"):
        assert var in css
    # Single-mode theme: no auto dark override.
    assert "prefers-color-scheme" not in css
    assert "color-scheme: dark" in css


def test_css_classic_keeps_dark_mode() -> None:
    css = themes.css(themes.THEMES["classic"])
    assert "@media (prefers-color-scheme: dark)" in css
    assert "color-scheme: light dark" in css
    # The dark block actually overrides the palette.
    assert css.count("--bg:") == 2


def test_css_is_balanced() -> None:
    for theme in themes.THEMES.values():
        css = themes.css(theme)
        assert css.count("{") == css.count("}"), theme.key
