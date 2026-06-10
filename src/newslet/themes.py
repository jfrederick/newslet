"""App-wide visual themes, selectable in the admin UI.

A :class:`Theme` is a small bundle of design tokens (colors, font stacks,
corner radii) consumed by two surfaces:

- the web templates, via :func:`css` → a ``:root { --bg: …; }`` variable
  block every page's stylesheet is written against;
- the email template, via inline-style interpolation of the same tokens
  (email clients ignore ``<style>`` classes, so the template reads the
  ``Theme`` directly).

"classic" reproduces the original look (including its automatic dark mode)
and is the default everywhere, so an unset/unknown stored theme changes
nothing. The other five riff on the textmode-revival aesthetic: monospace
type, square corners, terminal palettes.

Themes are code-defined constants — no user input ever flows into a token —
which is what makes interpolating them into CSS/inline styles safe.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

DEFAULT_THEME = "classic"

# Font stacks. Email-safe: every family here degrades gracefully in clients
# that only know the generic fallback. Deliberately unquoted (CSS allows
# unquoted multi-word family names) so the values survive Jinja's HTML
# autoescaping in the email template's inline styles without &#39; entities.
_SYSTEM_SANS = (
    "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
)
_SERIF = "Georgia,Times New Roman,serif"
_MONO = "ui-monospace,Menlo,Consolas,Courier New,monospace"


@dataclass(frozen=True)
class Palette:
    """One set of colors. Themes are either single-mode (a phosphor terminal
    is always dark) or carry a second palette for ``prefers-color-scheme``."""

    bg: str
    panel: str
    fg: str
    muted: str
    border: str
    border_soft: str  # hairline row dividers (email rows, list items)
    accent: str
    accent_fg: str  # text placed *on* an accent-colored surface
    accent_bg: str
    ok: str
    ok_bg: str
    ok_border: str
    bad: str
    bad_bg: str
    bad_border: str
    warn: str
    hn: str
    chip: str
    input_bg: str


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    font_body: str
    font_brand: str  # masthead + section headings
    font_mono: str
    radius: str  # cards, panels
    radius_sm: str  # buttons, inputs, vote chips
    color_scheme: str  # CSS color-scheme: "light", "dark", or "light dark"
    palette: Palette
    dark_palette: Palette | None = None  # auto dark-mode variant, if any


_CLASSIC = Theme(
    key="classic",
    label="Classic",
    font_body=_SYSTEM_SANS,
    font_brand=_SERIF,
    font_mono=_MONO,
    radius="10px",
    radius_sm="6px",
    color_scheme="light dark",
    palette=Palette(
        bg="#f6f6f4", panel="#ffffff", fg="#1a1a1a", muted="#888888",
        border="#e6e6e1", border_soft="#f1f1ec",
        accent="#0b3d91", accent_fg="#ffffff", accent_bg="#eef2fb",
        ok="#1a7a3a", ok_bg="#eef7ef", ok_border="#d4e8d8",
        bad="#9a2a2a", bad_bg="#fbeeee", bad_border="#e8d4d4",
        warn="#a06000", hn="#ff6600", chip="#f0f0ec", input_bg="#ffffff",
    ),
    dark_palette=Palette(
        bg="#15151a", panel="#1d1d22", fg="#ececec", muted="#9a9aa3",
        border="#2c2c33", border_soft="#26262c",
        accent="#8ab4ff", accent_fg="#10141f", accent_bg="#1b2436",
        ok="#6dc28a", ok_bg="#16271b", ok_border="#244a30",
        bad="#ff8a8a", bad_bg="#2a1c1c", bad_border="#4a2a2a",
        warn="#d99a4a", hn="#ff8c42", chip="#26262d", input_bg="#15151a",
    ),
)

_PHOSPHOR = Theme(
    key="phosphor",
    label="Phosphor",
    font_body=_MONO,
    font_brand=_MONO,
    font_mono=_MONO,
    radius="0",
    radius_sm="0",
    color_scheme="dark",
    palette=Palette(
        bg="#060c06", panel="#0a140a", fg="#bdf2c8", muted="#5e9a6c",
        border="#1d3a24", border_soft="#142a19",
        accent="#42e07a", accent_fg="#04140a", accent_bg="#0e2415",
        ok="#42e07a", ok_bg="#0e2415", ok_border="#1f4a2c",
        bad="#ff7a6b", bad_bg="#241010", bad_border="#4a221c",
        warn="#d8b04a", hn="#ffb000", chip="#10240f", input_bg="#060c06",
    ),
)

_AMBER = Theme(
    key="amber",
    label="Amber",
    font_body=_MONO,
    font_brand=_MONO,
    font_mono=_MONO,
    radius="0",
    radius_sm="0",
    color_scheme="dark",
    palette=Palette(
        bg="#0e0903", panel="#170f05", fg="#f2cf8e", muted="#9a7a48",
        border="#3a2a12", border_soft="#291d0c",
        accent="#ffb000", accent_fg="#1a1000", accent_bg="#241804",
        ok="#a8d65c", ok_bg="#16200a", ok_border="#2e4216",
        bad="#ff7a6b", bad_bg="#241008", bad_border="#4a2214",
        warn="#ffb000", hn="#ff8c42", chip="#221705", input_bg="#0e0903",
    ),
)

_PAPER = Theme(
    key="paper",
    label="Paper",
    font_body=_MONO,
    font_brand=_MONO,
    font_mono=_MONO,
    radius="0",
    radius_sm="0",
    color_scheme="light",
    palette=Palette(
        bg="#f2ecdd", panel="#faf6ea", fg="#2a261c", muted="#7d7460",
        border="#c9c0a8", border_soft="#e2dac4",
        accent="#a8402a", accent_fg="#faf6ea", accent_bg="#f2e0d8",
        ok="#3a6e3a", ok_bg="#e6eedd", ok_border="#bcd0ac",
        bad="#a8402a", bad_bg="#f2e0d8", bad_border="#dcc0b4",
        warn="#8a6200", hn="#b85c00", chip="#e8e0ca", input_bg="#faf6ea",
    ),
)

_DOS = Theme(
    key="dos",
    label="DOS",
    font_body=_MONO,
    font_brand=_MONO,
    font_mono=_MONO,
    radius="0",
    radius_sm="0",
    color_scheme="dark",
    palette=Palette(
        bg="#0014a8", panel="#0020c2", fg="#e8ecff", muted="#8fa2e8",
        border="#3a52d8", border_soft="#1c34cc",
        accent="#ffe14d", accent_fg="#101a60", accent_bg="#1430cc",
        ok="#54e89a", ok_bg="#0c2ab0", ok_border="#2a8a5e",
        bad="#ff8a7a", bad_bg="#1c20a0", bad_border="#a84a3e",
        warn="#ffe14d", hn="#ffb84d", chip="#1430cc", input_bg="#0014a8",
    ),
)

_MONO_INK = Theme(
    key="mono",
    label="Monochrome",
    font_body=_MONO,
    font_brand=_MONO,
    font_mono=_MONO,
    radius="0",
    radius_sm="0",
    color_scheme="light",
    palette=Palette(
        bg="#ffffff", panel="#ffffff", fg="#111111", muted="#666666",
        border="#111111", border_soft="#dddddd",
        accent="#111111", accent_fg="#ffffff", accent_bg="#f2f2f2",
        # Votes keep faint functional color so +/- stay tellable apart.
        ok="#0a6e30", ok_bg="#f2f2f2", ok_border="#111111",
        bad="#a02818", bad_bg="#f2f2f2", bad_border="#111111",
        warn="#6e5200", hn="#444444", chip="#eeeeee", input_bg="#ffffff",
    ),
)

THEMES: dict[str, Theme] = {
    t.key: t for t in (_CLASSIC, _PHOSPHOR, _AMBER, _PAPER, _DOS, _MONO_INK)
}


def get(name: str | None) -> Theme:
    """Resolve a stored theme name, falling back to classic.

    Lenient on read, like the rest of the config path: an unknown or empty
    name (e.g. a row written by a build that knew more themes) renders the
    default rather than erroring a page or blocking a send.
    """
    return THEMES.get(name or DEFAULT_THEME, THEMES[DEFAULT_THEME])


def list_themes() -> list[Theme]:
    """All themes in display order (for the admin picker)."""
    return list(THEMES.values())


def _vars(p: Palette, *, t: Theme) -> str:
    decls = [f"--{f.name.replace('_', '-')}: {getattr(p, f.name)};" for f in fields(Palette)]
    decls += [
        f"--font-body: {t.font_body};",
        f"--font-brand: {t.font_brand};",
        f"--font-mono: {t.font_mono};",
        f"--radius: {t.radius};",
        f"--radius-sm: {t.radius_sm};",
    ]
    return " ".join(decls)


def css(theme: Theme) -> str:
    """The ``:root`` CSS-variable block web templates style against.

    Includes a ``prefers-color-scheme`` override only when the theme has a
    dark variant (terminal themes are single-mode by design).
    """
    out = f":root {{ {_vars(theme.palette, t=theme)} }}\n"
    out += f"html {{ color-scheme: {theme.color_scheme}; }}\n"
    if theme.dark_palette is not None:
        out += (
            "@media (prefers-color-scheme: dark) { :root { "
            f"{_vars(theme.dark_palette, t=theme)} }} }}\n"
        )
    return out
