"""Shared theme, constants and small helpers for the ccusage plots.

Split out of ccusage_plot.py so the burn-rate panel (ccusage_burn.py)
and the main script (ccusage_plot.py) can share them without a circular
import. Plain sibling imports keep the main script directly executable
(`python ccusage_plot.py` puts its own directory on sys.path).
"""

from datetime import datetime, timezone
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib import ticker

# -- Theme colors --
BG_DARK = "#1a1a2e"
BG_AXES = "#16213e"
BORDER = "#2a2a4a"
TEXT = "#e0e0e0"
TEXT_DIM = "#8888aa"
GRID = "#2a2a4a"

COLORS = {
    "inputTokens": "#00d4aa",
    "outputTokens": "#ff8c42",
    "cacheCreateTokens": "#aa55ff",
    "cacheReadTokens": "#ff3366",
    "totalTokens": "#00d4ff",
    "costUSD": "#ffdd00",
}

PROJECTS_DIR = Path("/tmp/analyst.BCYKic3p/r2")

# -- Burn rate constants --
COLOR_LIMIT_HIT = "#ff3366"
COLOR_WINDOW = "#ffffff"

BURN_TOKEN_STYLES = {
    "output":       {"color": "#ee4444", "lw": 1.5, "alpha": 0.85, "label": "Output"},
    "input":        {"color": "#44dd66", "lw": 1.5, "alpha": 0.85, "label": "Input"},
    "cache_create": {"color": "#dd66aa", "lw": 1.5, "alpha": 0.85, "label": "Cache Create"},
    "cache_read":   {"color": "#44bbbb", "lw": 1.5, "alpha": 0.85, "label": "Cache Read"},
}

# Order the burn panel draws its series in.
BURN_TOKEN_KEYS = ["output", "input", "cache_create", "cache_read"]

MODEL_COLORS = {
    "opus-4-7": "#ff2222",
    "opus-4-6": "#ff8800",
    "opus-4-5": "#ffdd00",
    "sonnet-4-6": "#00bbff",
    "sonnet-4-5": "#8866ff",
    "haiku-4-5": "#88cc44",
}

WINDOW_GAP_S = 5 * 3600
SESSION_GAP_S = 1800
EMA_ALPHA = 0.15
BUCKET_MINUTES = 30
BUCKET_THRESHOLD = 20

# Chart definitions: (title, key, is_currency)
CHARTS = [
    ("Input Tokens", "inputTokens", False),
    ("Output Tokens", "outputTokens", False),
    ("Cache Create Tokens", "cacheCreateTokens", False),
    ("Cache Read Tokens", "cacheReadTokens", False),
    ("Total Tokens", "totalTokens", False),
    ("Cost (USD)", "costUSD", True),
]


def human_format(value, is_currency=False):
    prefix = "$" if is_currency else ""
    for suffix, threshold, fmt in [
        ("B", 1e9, ".2f"),
        ("M", 1e6, ".2f"),
        ("K", 1e3, ".1f"),
    ]:
        if abs(value) >= threshold:
            formatted = f"{value / threshold:{fmt}}"
            if "." in formatted:
                formatted = formatted.rstrip("0").rstrip(".")
            return f"{prefix}{formatted}{suffix}"
    if is_currency:
        return f"${value:,.2f}"
    return f"{int(value)}"


def make_formatter(is_currency):
    return ticker.FuncFormatter(lambda v, _: human_format(v, is_currency))


def apply_theme():
    plt.rcParams.update(
        {
            "figure.facecolor": BG_DARK,
            "axes.facecolor": BG_AXES,
            "axes.edgecolor": BORDER,
            "text.color": TEXT,
            "xtick.color": TEXT_DIM,
            "ytick.color": TEXT_DIM,
            "grid.color": GRID,
            "grid.alpha": 0.4,
            "font.family": "monospace",
        }
    )


def style_axes(ax):
    ax.set_facecolor(BG_AXES)
    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(1.5)
    ax.tick_params(colors=TEXT_DIM, labelsize=9)


def short_model(model):
    return model.replace("claude-", "").split("-2")[0]


TZ_ALIASES = {
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "PT": "America/Los_Angeles",
    "MST": "America/Denver",
    "MDT": "America/Denver",
    "MT": "America/Denver",
    "CST": "America/Chicago",
    "CDT": "America/Chicago",
    "CT": "America/Chicago",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "ET": "America/New_York",
    "GMT": "UTC",
    "UTC": "UTC",
    "BST": "Europe/London",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "IDT": "Asia/Jerusalem",
    "IST": "Asia/Kolkata",
    "JST": "Asia/Tokyo",
    "AEST": "Australia/Sydney",
    "AEDT": "Australia/Sydney",
}


def parse_event_ts(ts_raw):
    """JSONL timestamp (ISO string or unix millis) -> aware datetime, or
    None when the record carries no timestamp."""
    if not ts_raw:
        return None
    if isinstance(ts_raw, (int, float)):
        return datetime.fromtimestamp(ts_raw / 1000, tz=timezone.utc)
    return datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
