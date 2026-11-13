"""Resolve theme colors for use in Rich renderables.

Rich Text cannot reference CSS variables directly, so we resolve them from
the active theme object at render time. This means theme switches (built-in
themes or custom ones) apply immediately without restarting.

The app also auto-adopts the machine's active Omarchy theme (read from
~/.local/state/omarchy/current/theme.name + its colors.toml) and follows it
when it changes.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Optional

from textual.app import App
from textual.color import Color
from textual.theme import Theme

# theme attribute names we read straight off the Theme object
_DIRECT = (
    "accent",
    "primary",
    "secondary",
    "warning",
    "error",
    "success",
    "panel",
    "surface",
    "border",
    "foreground",
    "background",
)

_OMARCHY_STATE = Path.home() / ".local/state/omarchy/current/theme.name"
_OMARCHY_USER_THEMES = Path.home() / ".config/omarchy/themes"
_OMARCHY_SYSTEM_THEMES = Path("/usr/share/omarchy/themes")

# fallback palette (Gruvbox Material, omarchy's gruvbox) used when the
# machine theme cannot be read
_FALLBACK = {
    "mode": "dark",
    "accent": "#7daea3",
    "selection": "#504945",
    "muted": "#665c54",
    "background": "#282828",
    "dark_background": "#1e1e1e",
    "lighter_background": "#3c3836",
    "foreground": "#d4be98",
    "red": "#ea6962",
    "yellow": "#d8a657",
    "orange": "#e1875c",
    "green": "#a9b665",
    "cyan": "#89b482",
    "blue": "#7daea3",
    "magenta": "#d3869b",
}


def _rich_color(value: object) -> str:
    """Translate Textual color names into ones Rich understands.

    Textual's ANSI themes use names like ``ansi_blue`` / ``ansi_default``
    which Rich cannot parse; Rich expects ``blue`` / ``default``.
    """
    text = str(value)
    return text[5:] if text.startswith("ansi_") else text


def _blend_ramp(app: App, base: Color, steps: int) -> list[str]:
    """`steps` colors blending from the theme background up to `base`."""
    theme = app.current_theme
    bg = theme.background
    if not isinstance(bg, Color) or bg is None:
        bg = Color.parse("#1e1e1e")
    steps = max(1, steps)
    return [
        _rich_color(bg.blend(base, (i + 1) / steps).hex).lower()
        for i in range(steps)
    ]


def _parse_color(value: object) -> Color:
    """Parse a theme color name into a Textual Color.

    Accepts both Textual ANSI names (``ansi_blue``) and the Rich names this
    module hands back (``blue``, ``bright_red``, ``default``).
    """
    text = str(value)
    candidates = [text]
    if not text.startswith("ansi_"):
        candidates.append(f"ansi_{text}")
    for candidate in candidates:
        try:
            return Color.parse(candidate)
        except Exception:
            continue
    return Color.parse("#888888")


def color_ramp(app: App, var: str, steps: int) -> list[str]:
    """A list of `steps` colors from near-background to the full theme color.

    Used to give charts and bars a smooth intensity gradient: index 0 is the
    faintest (barely above the surface) and the last index is the full color.
    """
    theme = app.current_theme
    base = getattr(theme, var, None) or theme.foreground
    return _blend_ramp(app, _parse_color(base), steps)


def color_ramp_from(app: App, base_color: str | Color, steps: int) -> list[str]:
    """Like :func:`color_ramp` but blending up to an explicit color value."""
    return _blend_ramp(app, _parse_color(base_color), steps)


def theme_color(app: App, var: str) -> str:
    """Resolve a theme variable name to a color string usable in Rich styles.

    Supported names: the direct Theme fields (accent, primary, secondary,
    warning, error, success, foreground, ...) plus the derived ones used by
    the panels: "text", "text_muted" and "text_highlight".
    """
    theme = app.current_theme
    if var in _DIRECT:
        color = getattr(theme, var, None)
        if color is not None:
            return _rich_color(color)
    if var == "text":
        return _rich_color(theme.foreground or "default")
    if var == "text_muted":
        # "dim" is a terminal effect, so it works with any theme color
        return f"{_rich_color(theme.foreground or 'default')} dim"
    if var == "text_highlight":
        return f"{_rich_color(theme.foreground or 'default')} bold"
    # custom theme variables (from Theme.variables)
    custom = theme.variables.get(var)
    if custom is not None:
        return _rich_color(custom)
    return "default"


# ---------------------------------------------------------------------------
# Omarchy integration
# ---------------------------------------------------------------------------


def omarchy_active_theme() -> Optional[str]:
    """Name of the machine's active Omarchy theme, or None."""
    try:
        name = _OMARCHY_STATE.read_text().strip()
    except OSError:
        return None
    return name or None


def _omarchy_colors_path(name: str) -> Optional[Path]:
    for base in (_OMARCHY_USER_THEMES, _OMARCHY_SYSTEM_THEMES):
        path = base / name / "colors.toml"
        if path.exists():
            return path
    return None


def omarchy_signature() -> Optional[tuple[str, float]]:
    """Cheap change detector: (theme name, colors.toml mtime)."""
    name = omarchy_active_theme()
    if name is None:
        return None
    path = _omarchy_colors_path(name)
    try:
        mtime = path.stat().st_mtime if path else 0.0
    except OSError:
        mtime = 0.0
    return (name, mtime)


def _load_colors(name: str) -> dict:
    path = _omarchy_colors_path(name)
    if path is None:
        return dict(_FALLBACK)
    try:
        colors = tomllib.loads(path.read_text())
    except Exception:
        return dict(_FALLBACK)
    # fill missing keys from the fallback palette
    merged = dict(_FALLBACK)
    merged.update({k: v for k, v in colors.items() if isinstance(v, str)})
    return merged


def _reads_as_orange(value: str | None) -> bool:
    """True when a color sits in the orange band of the hue wheel.

    Some themes name a dark red "orange" (matte-black: #c63d3d), which makes
    warnings look red. Use this to pick a color that really reads as orange.
    """
    if not value:
        return False
    try:
        hsl = Color.parse(value).hsl
        hsv = Color.parse(value).hsv
    except Exception:
        return False
    return 0.03 <= hsl.h <= 0.16 and hsv.s >= 0.35


SYSTEM_THEME_NAME = "system"


def build_omarchy_theme(name: str, theme_name: str = SYSTEM_THEME_NAME) -> Optional[Theme]:
    """Build a Textual Theme from an Omarchy theme's colors.toml.

    It is registered as :data:`SYSTEM_THEME_NAME` so the theme cycle has a
    single, stable entry for "whatever the machine is using".
    """
    colors = _load_colors(name)
    bg = Color.parse(colors["background"])

    accent = colors["accent"]
    secondary = colors.get("cyan") or accent
    accent_col = colors.get("yellow") or accent

    # warning must read as orange: prefer the theme's orange, then a warm
    # yellow or the accent, and fall back to a neutral orange
    warning = next(
        (
            c
            for c in (colors.get("orange"), colors.get("yellow"), accent)
            if _reads_as_orange(c)
        ),
        "#e68e0d",
    )

    # subtle card border: the accent tinted toward the background
    card_border = Color.parse(accent).blend(bg, 0.62).hex
    card_border_hover = Color.parse(accent).blend(bg, 0.45).hex
    selection = colors.get("selection") or "#504945"

    return Theme(
        name=theme_name,
        primary=accent,
        secondary=secondary,
        accent=accent_col,
        warning=warning,
        error=colors.get("red"),
        success=colors.get("green"),
        foreground=colors.get("foreground"),
        background=colors.get("background"),
        surface=colors.get("dark_background"),
        panel=colors.get("lighter_background"),
        dark=colors.get("mode", "dark") == "dark",
        variables={
            # standard keys the CSS system honors: subtle card borders
            "border": card_border,
            "border-blurred": card_border,
            "scrollbar": card_border_hover,
            "scrollbar-hover": accent,
            "input-selection-background": f"{selection}80",
            "button-color-foreground": colors["background"],
            "block-cursor-foreground": colors.get("foreground", "#d4be98"),
        },
    )
