"""App-wide visual themes, selectable in the admin UI.

A :class:`Theme` is a small bundle of design tokens (colors, font stacks,
corner radii) consumed by two surfaces:

- the web templates, via :func:`css` → a ``:root { --bg: …; }`` variable
  block every page's stylesheet is written against;
- the email template, via inline-style interpolation of the same tokens
  (email clients ignore ``<style>`` classes, so the template reads the
  ``Theme`` directly).

"classic" reproduces the original look (including its automatic dark mode).
"foundry" — warm iron and molten-ember, in the style of the named design
directions Claude produces in chat — is the default for unset/unknown stored
themes; its siblings (atelier, manuscript, observatory, meadow) round out
that family, and the textmode-revival set (phosphor, amber, paper, dos,
mono) keeps its monospace terminal palettes.

Themes are code-defined constants — no user input ever flows into a token —
which is what makes interpolating them into CSS/inline styles safe.
"""

from __future__ import annotations

from dataclasses import dataclass, fields

DEFAULT_THEME = "foundry"

# Text-size bounds for the admin "Text size" dial (percent of the browser
# default). Shared by contracts.Config validation and the admin slider.
TEXT_SIZE_MIN = 75
TEXT_SIZE_MAX = 150
TEXT_SIZE_DEFAULT = 100

# Font stacks. Email-safe: every family here degrades gracefully in clients
# that only know the generic fallback. Deliberately unquoted (CSS allows
# unquoted multi-word family names) so the values survive Jinja's HTML
# autoescaping in the email template's inline styles without &#39; entities.
_SYSTEM_SANS = (
    "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
)
_SERIF = "Georgia,Times New Roman,serif"
_MONO = "ui-monospace,Menlo,Consolas,Courier New,monospace"
_GROTESK = "Futura,Avenir Next,Segoe UI,Roboto,Helvetica,Arial,sans-serif"
_DIDONE = "Didot,Bodoni MT,Playfair Display,Georgia,serif"
_BOOK_SERIF = "Iowan Old Style,Palatino,Palatino Linotype,Georgia,serif"


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

# --- The Claude-chat family: named design directions in the style Claude
# --- generates when asked to theme an app (Foundry and its siblings).

_FOUNDRY = Theme(
    key="foundry",
    label="Foundry",
    font_body=_SYSTEM_SANS,
    font_brand=_GROTESK,
    font_mono=_MONO,
    radius="4px",
    radius_sm="3px",
    color_scheme="light dark",
    # Workshop concrete with an ember accent by day…
    palette=Palette(
        bg="#f4f1ec", panel="#fcfaf6", fg="#211d18", muted="#847b6f",
        border="#ddd5c7", border_soft="#ece6da",
        accent="#c2410c", accent_fg="#fff7ed", accent_bg="#f9e7da",
        ok="#3f6212", ok_bg="#eef3e0", ok_border="#d3dfb4",
        bad="#9f1239", bad_bg="#fae6ec", bad_border="#eec4d2",
        warn="#92600a", hn="#ea580c", chip="#ece5d8", input_bg="#fcfaf6",
    ),
    # …cooling iron and molten metal after dark.
    dark_palette=Palette(
        bg="#16130f", panel="#1f1a14", fg="#ece4d8", muted="#9a8d7c",
        border="#363026", border_soft="#2a251d",
        accent="#ff8a3d", accent_fg="#1f1206", accent_bg="#33220f",
        ok="#a3c76d", ok_bg="#232a14", ok_border="#44512a",
        bad="#ff7d96", bad_bg="#2e1620", bad_border="#56263a",
        warn="#e0a94e", hn="#ff8c42", chip="#2a241b", input_bg="#16130f",
    ),
)

_ATELIER = Theme(
    key="atelier",
    label="Atelier",
    font_body=_SYSTEM_SANS,
    font_brand=_DIDONE,
    font_mono=_MONO,
    radius="12px",
    radius_sm="8px",
    color_scheme="light",
    palette=Palette(
        bg="#faf9f7", panel="#ffffff", fg="#1c1b1a", muted="#8a8581",
        border="#e8e4de", border_soft="#f1eee9",
        accent="#b0492f", accent_fg="#fdf5f1", accent_bg="#f7e8e2",
        ok="#2f6f4f", ok_bg="#e9f2ec", ok_border="#c8ddd0",
        bad="#a13434", bad_bg="#f7e7e5", bad_border="#e8c8c4",
        warn="#8f6a14", hn="#d97742", chip="#f0ece5", input_bg="#ffffff",
    ),
)

_MANUSCRIPT = Theme(
    key="manuscript",
    label="Manuscript",
    font_body=_BOOK_SERIF,
    font_brand=_BOOK_SERIF,
    font_mono=_MONO,
    radius="6px",
    radius_sm="4px",
    color_scheme="light",
    palette=Palette(
        bg="#f7f1e3", panel="#fffcf2", fg="#2f2618", muted="#8a7a5e",
        border="#e0d4b8", border_soft="#ede4cd",
        accent="#99342a", accent_fg="#fdf3ea", accent_bg="#f5e3dc",
        ok="#4a6b3a", ok_bg="#edf1e2", ok_border="#cfdbb9",
        bad="#99342a", bad_bg="#f5e3dc", bad_border="#e4c4b8",
        warn="#8a6200", hn="#b06a2a", chip="#efe6d0", input_bg="#fffcf2",
    ),
)

_OBSERVATORY = Theme(
    key="observatory",
    label="Observatory",
    font_body=_SYSTEM_SANS,
    font_brand=_SYSTEM_SANS,
    font_mono=_MONO,
    radius="10px",
    radius_sm="6px",
    color_scheme="dark",
    palette=Palette(
        bg="#0b1020", panel="#121a30", fg="#dbe4ff", muted="#8b97c0",
        border="#25304f", border_soft="#1b2440",
        accent="#7dd3fc", accent_fg="#062033", accent_bg="#11304a",
        ok="#6ee7a0", ok_bg="#0e2a1e", ok_border="#1e4a36",
        bad="#fda4af", bad_bg="#2c1622", bad_border="#4e2438",
        warn="#fbbf24", hn="#fb923c", chip="#1b2440", input_bg="#0b1020",
    ),
)

_MEADOW = Theme(
    key="meadow",
    label="Meadow",
    font_body=_SYSTEM_SANS,
    font_brand=_SYSTEM_SANS,
    font_mono=_MONO,
    radius="12px",
    radius_sm="8px",
    color_scheme="light",
    palette=Palette(
        bg="#f2f5ec", panel="#fbfdf7", fg="#232a1e", muted="#76836b",
        border="#d9e0cc", border_soft="#e9eedd",
        accent="#38703c", accent_fg="#f3faf0", accent_bg="#e3efe0",
        ok="#38703c", ok_bg="#e3efe0", ok_border="#c1d8c0",
        bad="#a8442f", bad_bg="#f6e6e0", bad_border="#e3c4b8",
        warn="#8a6d1a", hn="#c2762a", chip="#e7ecda", input_bg="#fbfdf7",
    ),
)

THEMES: dict[str, Theme] = {
    t.key: t
    for t in (
        _FOUNDRY,
        _ATELIER,
        _MANUSCRIPT,
        _OBSERVATORY,
        _MEADOW,
        _CLASSIC,
        _PHOSPHOR,
        _AMBER,
        _PAPER,
        _DOS,
        _MONO_INK,
    )
}


def get(name: str | None) -> Theme:
    """Resolve a stored theme name, falling back to the default (foundry).

    Lenient on read, like the rest of the config path: an unknown or empty
    name (e.g. a row written by a build that knew more themes) renders the
    default rather than erroring a page or blocking a send. Callers that
    need a *historically accurate* fallback (the as-sent email archive)
    pass an explicit name instead of relying on this default.
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


def css(theme: Theme, text_size: int = TEXT_SIZE_DEFAULT) -> str:
    """The ``:root`` CSS-variable block web templates style against.

    Includes a ``prefers-color-scheme`` override only when the theme has a
    dark variant (terminal themes are single-mode by design). ``text_size``
    (percent) scales the root font size; templates declare type in ``rem``
    so the whole page follows the admin's text-size dial. Clamped here as a
    backstop — the stored value is validated by ``contracts.Config``.
    """
    text_size = min(max(int(text_size), TEXT_SIZE_MIN), TEXT_SIZE_MAX)
    out = f":root {{ {_vars(theme.palette, t=theme)} }}\n"
    out += f"html {{ color-scheme: {theme.color_scheme}; font-size: {text_size}%; }}\n"
    if theme.dark_palette is not None:
        out += (
            "@media (prefers-color-scheme: dark) { :root { "
            f"{_vars(theme.dark_palette, t=theme)} }} }}\n"
        )
    return out
