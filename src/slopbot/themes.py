from __future__ import annotations

from dataclasses import dataclass

THEME_PRESETS = frozenset({"clear-blue", "quiet-green", "soft-indigo", "xkcd"})
THEME_MODES = frozenset({"system", "light", "dark"})


@dataclass(frozen=True)
class ChartTheme:
    background: str
    panel: str
    grid: str
    text: str
    muted: str
    line: str
    fill: str
    sketch: bool = False


_LEGACY_CHART_THEME = ChartTheme(
    background="#111827",
    panel="#172033",
    grid="#344155",
    text="#e5e7eb",
    muted="#94a3b8",
    line="#60a5fa",
    fill="#1e3a5f",
)

_CHART_THEMES = {
    ("clear-blue", "light"): ChartTheme(
        "#f7f8fa", "#ffffff", "#dde1e7", "#15171a", "#626871", "#2563eb", "#dbe7ff"
    ),
    ("clear-blue", "dark"): ChartTheme(
        "#18191c", "#222428", "#383c43", "#f3f4f6", "#a7adb6", "#78a7ff", "#263b60"
    ),
    ("quiet-green", "light"): ChartTheme(
        "#f5f7f5", "#ffffff", "#d8ded9", "#171a18", "#646b66", "#087a50", "#d9eee5"
    ),
    ("quiet-green", "dark"): ChartTheme(
        "#161817", "#1e211f", "#373d39", "#f3f4f6", "#a7adb6", "#63c99e", "#24463a"
    ),
    ("soft-indigo", "light"): ChartTheme(
        "#f7f7fb", "#ffffff", "#dedeea", "#1b1b25", "#666677", "#4f46c8", "#e1e0f8"
    ),
    ("soft-indigo", "dark"): ChartTheme(
        "#18181d", "#222229", "#3a3a47", "#f3f4f6", "#a7adb6", "#aaa4ff", "#383558"
    ),
    ("xkcd", "light"): ChartTheme(
        "#fffdf5", "#fffdf5", "#bbb5a7", "#191815", "#635f56", "#276fbf", "#dbe9f5", True
    ),
    ("xkcd", "dark"): ChartTheme(
        "#11110f", "#191916", "#4c493f", "#f6f0df", "#b9b2a3", "#78b7f2", "#243c50", True
    ),
}


def chart_theme(preset: str | None, mode: str | None) -> ChartTheme:
    """Return a Discord chart theme, preserving the historical default when unset."""
    if preset not in THEME_PRESETS:
        return _LEGACY_CHART_THEME
    # A Discord interaction cannot observe the caller's OS color scheme. Treat
    # the website's "system" choice as light unless the user explicitly chose dark.
    resolved_mode = "dark" if mode == "dark" else "light"
    return _CHART_THEMES[(preset, resolved_mode)]
