#!/usr/bin/env python3
"""Plot Claude Code usage data by reading local conversation logs directly."""

__version__ = "1.3.0"

import argparse
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from itertools import accumulate
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from matplotlib import gridspec

from ccusage_burn import (
    build_sessions,
    find_limit_hits,
    find_window_boundaries,
    plot_burn_rate,
)
from ccusage_common import (
    BG_AXES,
    BG_DARK,
    BORDER,
    CHARTS,
    COLORS,
    GRID,
    PROJECTS_DIR,
    TEXT,
    TEXT_DIM,
    TZ_ALIASES,
    apply_theme,
    human_format,
    make_formatter,
    parse_event_ts,
    style_axes,
)


def parse_period(period_str):
    m = re.fullmatch(r"(\d+)\s*([hdwm])", period_str.strip().lower())
    if not m:
        print(
            f"Error: invalid period '{period_str}'. Use e.g. 6h, 3d, 1w, 2m",
            file=sys.stderr,
        )
        sys.exit(1)
    value, unit = int(m.group(1)), m.group(2)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "w":
        return timedelta(weeks=value)
    # The [hdwm] pattern leaves only "m".
    return timedelta(days=value * 30)


def parse_datetime(dt_str, tz=None):
    """Parse 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM' into a timezone-aware datetime."""
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(dt_str.strip(), fmt)
            if tz:
                return dt.replace(tzinfo=tz)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    print(
        f"Error: invalid date '{dt_str}'. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
        file=sys.stderr,
    )
    sys.exit(1)


# Approximate cost per token by model (USD)
# Tuple: (input, output, cache_create_5m, cache_create_1h, cache_read)
# 5-minute ephemeral cache writes are billed at 1.25x base input;
# 1-hour ephemeral cache writes are billed at 2x base input.
MODEL_PRICING = {
    "claude-opus-4-7":           (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-opus-4-6":           (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-opus-4-5-20251101":  (5 / 1e6, 25 / 1e6, 6.25 / 1e6, 10 / 1e6,  0.5 / 1e6),
    "claude-sonnet-4-6":         (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6,   0.3 / 1e6),
    "claude-sonnet-4-5-20250929": (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6,  0.3 / 1e6),
    "claude-haiku-4-5-20251001": (1 / 1e6, 5 / 1e6,  1.25 / 1e6, 2 / 1e6,   0.1 / 1e6),
}
# (input, output, cache_create_5m, cache_create_1h, cache_read)
DEFAULT_PRICING = (3 / 1e6, 15 / 1e6, 3.75 / 1e6, 6 / 1e6, 0.3 / 1e6)


def estimate_cost(model, input_t, output_t,
                  cache_create_5m_t, cache_create_1h_t, cache_read_t):
    """Cost split by cache TTL.

    The Anthropic API reports ephemeral cache writes in two buckets
    (5-minute and 1-hour) under `usage.cache_creation`. They are billed
    at different rates, so we keep them separate. Older callers that pass
    a single combined `cache_create` value should split it themselves;
    a back-compat shim is provided as `estimate_cost_legacy` below.
    """
    pricing = DEFAULT_PRICING
    for prefix, p in MODEL_PRICING.items():
        if model and model.startswith(prefix.rsplit("-", 1)[0]):
            pricing = p
            break
    pi, po, pcc5, pcc1h, pcr = pricing
    return (
        input_t * pi
        + output_t * po
        + cache_create_5m_t * pcc5
        + cache_create_1h_t * pcc1h
        + cache_read_t * pcr
    )


def estimate_cost_legacy(model, input_t, output_t, cache_create_t, cache_read_t):
    """Back-compat: assume all cache_create is 5-minute ephemeral."""
    return estimate_cost(model, input_t, output_t, cache_create_t, 0, cache_read_t)


def _usage_tokens(usage):
    """(input, output, cache_create, cache_read, create_5m, create_1h)."""
    input_t = usage.get("input_tokens", 0) or 0
    output_t = usage.get("output_tokens", 0) or 0
    cache_create = usage.get("cache_creation_input_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    # Split cache_create into 5-minute / 1-hour ephemeral buckets.
    # The API reports them separately under `usage.cache_creation`.
    # Any leftover (cache_create - 5m - 1h) is treated as 5m by
    # default — that's the historical/implicit TTL.
    cc_detail = usage.get("cache_creation") or {}
    create_5m = cc_detail.get("ephemeral_5m_input_tokens", 0) or 0
    create_1h = cc_detail.get("ephemeral_1h_input_tokens", 0) or 0
    create_5m += max(0, cache_create - create_5m - create_1h)
    return input_t, output_t, cache_create, cache_read, create_5m, create_1h


def _new_event(ts, model, toks):
    input_t, output_t, cache_create, cache_read, create_5m, create_1h = toks
    return {
        "timestamp": ts,
        "model": model,
        "inputTokens": input_t,
        "outputTokens": output_t,
        "cacheCreateTokens": cache_create,
        "cacheCreate5mTokens": create_5m,
        "cacheCreate1hTokens": create_1h,
        "cacheReadTokens": cache_read,
        "totalTokens": input_t + output_t + cache_create + cache_read,
        "costUSD": estimate_cost(
            model, input_t, output_t, create_5m, create_1h, cache_read,
        ),
    }


def _merge_event(ev, toks):
    """Fold a streaming chunk into its request's event.

    Claude Code splits one logical API response into N JSONL records
    (thinking + text + tool_use blocks, streaming chunks). All N share the
    same `requestId`; input / cache_create / cache_read are bit-identical
    across them; only `output_tokens` may grow as streaming progresses
    (intermediate records report partial counts, the final carries the
    aggregate). Take max per usage field — correct for both the identical
    fields and the streaming-output case.
    """
    input_t, output_t, cache_create, cache_read, create_5m, create_1h = toks
    ev["inputTokens"] = max(ev["inputTokens"], input_t)
    ev["outputTokens"] = max(ev["outputTokens"], output_t)
    ev["cacheCreateTokens"] = max(ev["cacheCreateTokens"], cache_create)
    ev["cacheCreate5mTokens"] = max(ev["cacheCreate5mTokens"], create_5m)
    ev["cacheCreate1hTokens"] = max(ev["cacheCreate1hTokens"], create_1h)
    ev["cacheReadTokens"] = max(ev["cacheReadTokens"], cache_read)
    ev["totalTokens"] = (
        ev["inputTokens"] + ev["outputTokens"]
        + ev["cacheCreateTokens"] + ev["cacheReadTokens"]
    )
    ev["costUSD"] = estimate_cost(
        ev["model"],
        ev["inputTokens"], ev["outputTokens"],
        ev["cacheCreate5mTokens"], ev["cacheCreate1hTokens"],
        ev["cacheReadTokens"],
    )


def _record_ts(obj, seen_uuids):
    """Event timestamp for an assistant record, or None when it is a
    cross-file duplicate or carries no timestamp.

    Cross-file dedup by record uuid. The SAME API call can appear in
    multiple jsonls — most commonly a session's main `<uuid>.jsonl` plus
    its `data/subagents/agent-*.jsonl` companion — with identical inner
    `uuid` but different wrappers. Without this, subagent tokens/cost
    double-count. Records without a `uuid` (legacy data) pass through.
    """
    rec_uuid = obj.get("uuid")
    if rec_uuid:
        if rec_uuid in seen_uuids:
            return None
        seen_uuids.add(rec_uuid)
    return parse_event_ts(obj.get("timestamp"))


def _file_events(path, seen_uuids, cutoff, end):
    """Assistant usage events from one JSONL file, merged by requestId."""
    events = []
    seen_request_events: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                if obj.get("type") != "assistant":
                    continue
                ts = _record_ts(obj, seen_uuids)
                if ts is None:
                    continue
                if cutoff and ts < cutoff:
                    continue
                if end and ts > end:
                    continue
                msg = obj.get("message", {})
                usage = msg.get("usage", {})
                if not usage:
                    continue
                toks = _usage_tokens(usage)
                req_id = obj.get("requestId", "")
                if req_id and req_id in seen_request_events:
                    _merge_event(seen_request_events[req_id], toks)
                    continue
                ev = _new_event(ts, msg.get("model", "unknown"), toks)
                if req_id:
                    seen_request_events[req_id] = ev
                events.append(ev)
    except (json.JSONDecodeError, KeyError, OSError):
        pass
    return events


def load_events(cutoff=None, end=None):
    """Read conversation JSONL files and extract assistant message usage data."""
    if not PROJECTS_DIR.exists():
        print(f"Error: projects dir not found: {PROJECTS_DIR}", file=sys.stderr)
        sys.exit(1)

    jsonl_files = list(PROJECTS_DIR.rglob("*.jsonl"))
    print(f"Scanning {len(jsonl_files)} conversation files...", file=sys.stderr)

    seen_uuids: set[str] = set()
    events = []
    for path in jsonl_files:
        events.extend(_file_events(path, seen_uuids, cutoff, end))

    events.sort(key=lambda e: e["timestamp"])
    return events


def _check_tzdata():
    """Ensure timezone data is available (needed on Windows)."""
    try:
        ZoneInfo("UTC")
    except Exception:
        print(
            "Error: timezone database not found. On Windows, install it with:\n"
            "  pip install tzdata",
            file=sys.stderr,
        )
        sys.exit(1)


def resolve_tz(tz_str: str | None) -> ZoneInfo | None:
    """Resolve a timezone string (alias or IANA name) to a ZoneInfo object."""
    if tz_str is None:
        return None
    _check_tzdata()
    key = tz_str.upper()
    iana_key = TZ_ALIASES.get(key, tz_str)
    try:
        return ZoneInfo(iana_key)
    except KeyError:
        print(
            f"Error: unknown timezone '{tz_str}'. Use e.g. PST, EST, UTC, Asia/Tokyo",
            file=sys.stderr,
        )
        sys.exit(1)


def _get_plan_from_credentials():
    """Fallback: read subscription type from .credentials.json (Windows)."""
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if creds_path.exists():
        try:
            with open(creds_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                plan = None
                if "claudeAiOauth" in data:
                    plan = data["claudeAiOauth"].get("subscriptionType")
                if plan:
                    return str(plan).capitalize()
        except Exception:
            pass
    return None


def get_claude_info():
    """Get subscription type and version from the claude CLI, with credentials.json fallback."""
    plan = ""
    version = ""
    try:
        # check=False: the CLI's exit code is not meaningful here — a
        # non-zero status still leaves stdout to try, and JSONDecodeError
        # below handles an empty one.
        result = subprocess.run(
            ["claude", "auth", "status"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        data = json.loads(result.stdout)
        p = data.get("subscriptionType", "")
        if p:
            plan = str(p).capitalize()
    except (subprocess.SubprocessError, json.JSONDecodeError, FileNotFoundError):
        # CLI not available (common on Windows), fall back to credentials file
        creds_plan = _get_plan_from_credentials()
        if creds_plan:
            plan = creds_plan
    try:
        # check=False: as above; a bare stdout is all this reads.
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        version = result.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
    return plan, version


HIGHLIGHT_COLOR = "#ffffff"
HIGHLIGHT_ALPHA = 0.06


def parse_highlight(highlight_str):
    """Parse '5-11' or '5:00-11:00' into (start_hour, end_hour) as floats."""
    m = re.fullmatch(
        r"(\d{1,2})(?::(\d{2}))?-(\d{1,2})(?::(\d{2}))?", highlight_str.strip()
    )
    if not m:
        print(
            f"Error: invalid highlight '{highlight_str}'. Use e.g. 5-11 or 5:00-11:30",
            file=sys.stderr,
        )
        sys.exit(1)
    sh = int(m.group(1)) + (int(m.group(2)) / 60 if m.group(2) else 0)
    eh = int(m.group(3)) + (int(m.group(4)) / 60 if m.group(4) else 0)
    return sh, eh


def add_highlight_bands(ax, timestamps, start_hour, end_hour, tz):
    """Add vertical shaded bands for each day's highlight window, clipped to current xlim."""
    if not timestamps:
        return
    display_tz = tz if tz else timezone.utc

    # Save current x-axis limits before adding spans
    xlim = ax.get_xlim()

    dates_seen = set()
    for ts in timestamps:
        dt = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        dt = dt.astimezone(display_tz)
        dates_seen.add(dt.date())

    for d in sorted(dates_seen):
        band_start = datetime(
            d.year,
            d.month,
            d.day,
            int(start_hour),
            int((start_hour % 1) * 60),
            tzinfo=display_tz,
        )
        band_end = datetime(
            d.year,
            d.month,
            d.day,
            int(end_hour),
            int((end_hour % 1) * 60),
            tzinfo=display_tz,
        )
        ax.axvspan(
            band_start, band_end, alpha=HIGHLIGHT_ALPHA, color=HIGHLIGHT_COLOR, zorder=1
        )

    # Restore x-axis limits so highlight bands don't expand the view
    ax.set_xlim(xlim)


class _TimelineCtx(NamedTuple):
    """Per-figure state the chart panels share."""
    timestamps: list
    bin_delta: timedelta
    bin_label: str
    span_h: float
    fmt_tz: object
    highlight: object
    tz: object


def _tz_label(tz):
    """Short display label for a resolved timezone (alias when known)."""
    if not tz:
        return "UTC"
    for alias, iana in TZ_ALIASES.items():
        if iana == str(tz):
            return alias
    return str(tz)


def _date_range_str(timestamps, tz, tz_label):
    display_tz = tz if tz else timezone.utc
    first_ts = (
        timestamps[0]
        if timestamps[0].tzinfo
        else timestamps[0].replace(tzinfo=display_tz)
    )
    last_ts = (
        timestamps[-1]
        if timestamps[-1].tzinfo
        else timestamps[-1].replace(tzinfo=display_tz)
    )
    return (
        f"{first_ts.strftime('%b %d %H:%M')} – "
        f"{last_ts.strftime('%b %d %H:%M')} {tz_label}"
    )


def _figure_header(fig, ctx, events):
    """Suptitle (plan/version) + subtitle (range, calls, cost)."""
    tz_label = _tz_label(ctx.tz)
    date_range_str = _date_range_str(ctx.timestamps, ctx.tz, tz_label)

    plan_name, claude_version = get_claude_info()

    title_parts = ["Claude Code Usage"]
    if plan_name:
        title_parts.append(f"Plan: {plan_name}")
    if claude_version:
        title_parts.append(f"v{claude_version.split()[0]}")
    fig.suptitle(
        "  |  ".join(title_parts),
        fontsize=18, fontweight="bold", color="#ffffff", y=0.99,
    )
    subtitle_parts = [
        date_range_str,
        f"{len(events)} API calls",
        f"${sum(e['costUSD'] for e in events):.2f} total",
    ]
    if ctx.highlight:
        subtitle_parts.append(
            f"Highlight: {int(ctx.highlight[0])}:00–{int(ctx.highlight[1])}:00"
        )
    fig.text(
        0.5,
        0.96,
        "  |  ".join(subtitle_parts),
        ha="center",
        fontsize=11,
        color=TEXT_DIM,
    )


# Target bar count regardless of time range.
_TARGET_BARS = 120

# (seconds, label) candidates the bin width snaps to.
_CLEAN_INTERVALS = [
    (60, "per 1min"), (120, "per 2min"), (300, "per 5min"),
    (600, "per 10min"), (900, "per 15min"), (1800, "per 30min"),
    (3600, "per 1h"), (7200, "per 2h"), (14400, "per 4h"),
    (21600, "per 6h"), (43200, "per 12h"), (86400, "per 1d"),
    (604800, "per 1w"), (2592000, "per 30d"),
]


def _timeline_ctx(events, tz, highlight):
    if tz:
        timestamps = [e["timestamp"].astimezone(tz) for e in events]
    else:
        timestamps = [e["timestamp"] for e in events]

    # Determine time span and bin size
    span_h = (
        (timestamps[-1] - timestamps[0]).total_seconds() / 3600
        if len(timestamps) > 1
        else 1
    )
    bin_seconds = max(60, span_h * 3600 / _TARGET_BARS)
    # Snap to a clean interval
    bin_delta = timedelta(seconds=_CLEAN_INTERVALS[-1][0])
    bin_label = _CLEAN_INTERVALS[-1][1]
    for secs, label in _CLEAN_INTERVALS:
        if secs >= bin_seconds:
            bin_delta = timedelta(seconds=secs)
            bin_label = label
            break

    return _TimelineCtx(
        timestamps=timestamps,
        bin_delta=bin_delta,
        bin_label=bin_label,
        span_h=span_h,
        fmt_tz=tz if tz else timezone.utc,
        highlight=highlight,
        tz=tz,
    )


def _panel_bins(timestamps, values, bin_delta):
    """Bin events into time segments: (bin_starts, bin_totals)."""
    bin_starts = []
    bin_totals = []
    bin_start = timestamps[0]
    bin_sum = 0
    ts_idx = 0
    while bin_start <= timestamps[-1]:
        bin_end = bin_start + bin_delta
        while ts_idx < len(timestamps) and timestamps[ts_idx] < bin_end:
            bin_sum += values[ts_idx]
            ts_idx += 1
        bin_starts.append(bin_start)
        bin_totals.append(bin_sum)
        bin_sum = 0
        bin_start = bin_end
    return bin_starts, bin_totals


def _panel_xaxis(ax, ctx):
    if ctx.span_h <= 6:
        ax.xaxis.set_major_locator(mdates.HourLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ctx.fmt_tz))
    elif ctx.span_h <= 24:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=ctx.fmt_tz))
    elif ctx.span_h <= 24 * 3:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M", tz=ctx.fmt_tz))
    elif ctx.span_h <= 24 * 7:
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=ctx.fmt_tz))
    elif ctx.span_h <= 24 * 60:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=ctx.fmt_tz))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y", tz=ctx.fmt_tz))
    ax.tick_params(axis="x", rotation=0, labelsize=8)


def _plot_chart_panel(ax, ctx, title, key, is_currency, events):
    values = [e[key] for e in events]
    color = COLORS[key]

    bin_starts, bin_totals = _panel_bins(
        ctx.timestamps, values, ctx.bin_delta
    )

    # Bar width fills bin with small gap (in days, the x-axis unit)
    bar_width = (ctx.bin_delta * 0.9) / timedelta(days=1)
    ax.bar(
        bin_starts, bin_totals,
        width=bar_width, color=color, alpha=0.3, align="edge", zorder=3,
    )

    # Cumulative line on secondary y-axis
    cumulative = list(accumulate(values))

    ax2 = ax.twinx()
    ax2.plot(ctx.timestamps, cumulative, color="#ffffff", alpha=0.15, linewidth=4, zorder=4)
    ax2.plot(ctx.timestamps, cumulative, color=color, alpha=1.0, linewidth=2, zorder=5)
    ax2.fill_between(ctx.timestamps, cumulative, alpha=0.04, color=color, zorder=2)
    ax2.yaxis.set_major_formatter(make_formatter(is_currency))
    ax2.tick_params(colors=TEXT_DIM, labelsize=8)
    ax2.spines["right"].set_color(BORDER)

    if cumulative:
        total_val = cumulative[-1]
        ax2.annotate(
            f"Total: {human_format(total_val, is_currency)}",
            xy=(ctx.timestamps[-1], total_val),
            xytext=(-10, 8),
            textcoords="offset points",
            fontsize=10,
            color=color,
            fontweight="bold",
            ha="right",
            va="bottom",
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": BG_AXES,
                "edgecolor": color,
                "alpha": 0.8,
            },
        )

    ax.set_title(title, fontsize=13, fontweight="bold", color=TEXT, pad=10)
    ax.yaxis.set_major_formatter(make_formatter(is_currency))
    ax.set_ylabel(ctx.bin_label, fontsize=8, color=TEXT_DIM)
    ax2.set_ylabel("cumulative", fontsize=8, color=TEXT_DIM)
    ax.grid(True, alpha=0.2, color=GRID)

    if ctx.highlight:
        add_highlight_bands(
            ax, ctx.timestamps, ctx.highlight[0], ctx.highlight[1], ctx.tz
        )

    _panel_xaxis(ax, ctx)


def _model_costs(events):
    """(models sorted by cost desc, costs, reqs, short names, bar colors)."""
    model_costs = {}
    model_reqs = {}
    for e in events:
        m = e["model"]
        model_costs[m] = model_costs.get(m, 0) + e["costUSD"]
        model_reqs[m] = model_reqs.get(m, 0) + 1
    models = sorted(model_costs.keys(), key=lambda m: model_costs[m], reverse=True)
    bar_colors = list(COLORS.values())
    costs = [model_costs[m] for m in models]
    short_names = [m.replace("claude-", "").split("-2")[0] for m in models]
    colors = [bar_colors[i % len(bar_colors)] for i in range(len(models))]
    return models, costs, model_reqs, short_names, colors


def _plot_summary_panel(ax, events):
    """Cost by model panel."""
    models, costs, model_reqs, short_names, colors = _model_costs(events)
    y_pos = list(range(len(models)))

    bars = ax.barh(y_pos, costs, color=colors, alpha=0.85, height=0.5, zorder=3)
    for rect, m, val in zip(bars, models, costs):
        reqs = model_reqs[m]
        ax.text(
            rect.get_width() + max(costs) * 0.02,
            rect.get_y() + rect.get_height() / 2,
            f"${val:.2f} ({reqs} calls)",
            va="center",
            ha="left",
            fontsize=10,
            color=TEXT,
            fontweight="bold",
        )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(short_names, fontsize=10)
    ax.set_title(
        "Cost by Model", fontsize=13, fontweight="bold", color=TEXT, pad=10
    )
    ax.xaxis.set_major_formatter(make_formatter(True))
    ax.grid(True, axis="x", alpha=0.3, color=GRID)
    ax.invert_yaxis()
    if costs and max(costs) > 0:
        ax.set_xlim(0, max(costs) * 1.4)


def _plot_breakdown_panel(ax, events):
    """Token breakdown panel."""
    token_categories = [
        ("Input", "inputTokens", COLORS["inputTokens"]),
        ("Output", "outputTokens", COLORS["outputTokens"]),
        ("Cache Create", "cacheCreateTokens", COLORS["cacheCreateTokens"]),
        ("Cache Read", "cacheReadTokens", COLORS["cacheReadTokens"]),
    ]
    cat_labels = [c[0] for c in token_categories]
    cat_totals = [sum(e[c[1]] for e in events) for c in token_categories]
    cat_colors = [c[2] for c in token_categories]
    y_pos = list(range(len(cat_labels)))

    bars = ax.barh(
        y_pos, cat_totals, color=cat_colors, alpha=0.85, height=0.5, zorder=3
    )
    for rect, total in zip(bars, cat_totals):
        if total > 0:
            pct = total / sum(cat_totals) * 100 if sum(cat_totals) > 0 else 0
            ax.text(
                rect.get_width() + max(cat_totals) * 0.02,
                rect.get_y() + rect.get_height() / 2,
                f"{human_format(total)} ({pct:.1f}%)",
                va="center",
                ha="left",
                fontsize=10,
                color=TEXT,
                fontweight="bold",
            )

    ax.set_yticks(y_pos)
    ax.set_yticklabels(cat_labels, fontsize=10)
    ax.set_title(
        "Token Breakdown", fontsize=13, fontweight="bold", color=TEXT, pad=10
    )
    ax.xaxis.set_major_formatter(make_formatter(False))
    ax.grid(True, axis="x", alpha=0.3, color=GRID)
    ax.invert_yaxis()
    if cat_totals and max(cat_totals) > 0:
        ax.set_xlim(0, max(cat_totals) * 1.35)


def _plot_panels(axes, ctx, events):
    for idx, (title, key, is_currency) in enumerate(CHARTS):
        _plot_chart_panel(axes[idx], ctx, title, key, is_currency, events)
    _plot_summary_panel(axes[len(CHARTS)], events)
    _plot_breakdown_panel(axes[len(CHARTS) + 1], events)
    # Hide unused axes slots
    for i in range(len(CHARTS) + 2, len(axes)):
        axes[i].set_visible(False)


def _plot_burn_panel(ax_burn, events):
    """Burn rate panel (full width, bottom row)."""
    style_axes(ax_burn)
    sessions = build_sessions(events)
    if sessions:
        window_boundaries = find_window_boundaries(events)
        limit_hits = find_limit_hits(events)
        plot_burn_rate(ax_burn, events, sessions, window_boundaries, limit_hits,
                       view_start=events[0]["timestamp"],
                       view_end=events[-1]["timestamp"])


def plot_timeline(events, period_str, output_path, tz=None, highlight=None):
    apply_theme()
    ctx = _timeline_ctx(events, tz, highlight)

    fig = plt.figure(figsize=(18, 26))
    gs_top = gridspec.GridSpec(4, 2, figure=fig,
                               top=0.94, bottom=0.27, hspace=0.35, wspace=0.3)
    gs_burn = gridspec.GridSpec(1, 1, figure=fig,
                                top=0.21, bottom=0.03)
    axes = [fig.add_subplot(gs_top[r, c]) for r in range(4) for c in range(2)]
    ax_burn = fig.add_subplot(gs_burn[0])

    _figure_header(fig, ctx, events)
    _plot_panels(axes, ctx, events)
    _plot_burn_panel(ax_burn, events)

    fig.savefig(output_path, dpi=150, bbox_inches="tight", facecolor=BG_DARK)
    plt.close()
    print(f"Saved: {output_path}", file=sys.stderr)


SCRIPT_URL = "https://raw.githubusercontent.com/nhz-io/ccusage-plot/main/ccusage_plot.py"


def _resolve_script_path():
    """Find the real path of this script, resolving symlinks and verifying identity."""
    # resolve() follows symlinks and makes the path absolute
    candidate = Path(__file__).resolve()

    # If running via stdin (curl pipe), __file__ won't be a real path
    if not candidate.is_file():
        return None

    # Verify this is actually our script by checking for our version string
    try:
        content = candidate.read_text(encoding="utf-8")
        if f'__version__ = "{__version__}"' not in content:
            return None
    except Exception:
        return None

    return candidate


def check_update(target_path=None):
    """Check for a newer version and auto-update if available."""
    script_path = Path(target_path).resolve() if target_path else _resolve_script_path()

    if script_path is None:
        print(
            "Error: cannot determine script location (running via pipe?).\n"
            "Use: --update /path/to/ccusage_plot.py",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Script location: {script_path}", file=sys.stderr)

    try:
        with urllib.request.urlopen(SCRIPT_URL, timeout=10) as resp:
            remote_source = resp.read().decode("utf-8")
    except Exception as e:
        print(f"Error checking for updates: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract remote version
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', remote_source, re.MULTILINE)
    if not m:
        print("Error: could not determine remote version.", file=sys.stderr)
        sys.exit(1)

    remote_version = m.group(1)
    if remote_version == __version__:
        print(f"Already up to date (v{__version__}).", file=sys.stderr)
        sys.exit(0)

    # Update in place
    try:
        script_path.write_text(remote_source, encoding="utf-8")
        # Set executable bit on Unix (no-op on Windows)
        if sys.platform != "win32":
            script_path.chmod(script_path.stat().st_mode | 0o111)
        print(f"Updated: v{__version__} -> v{remote_version}", file=sys.stderr)
    except Exception as e:
        print(f"Error writing update: {e}", file=sys.stderr)


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Plot Claude Code usage from local conversation logs"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--update",
        nargs="?",
        const=True,
        default=None,
        metavar="PATH",
        help="Auto-update to the latest version from GitHub. Optionally specify script path.",
    )
    parser.add_argument(
        "-p",
        "--period",
        default=None,
        help="Time period, e.g. 6h, 3d, 1w, 2m (default: 24h)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Plot all history",
    )
    parser.add_argument(
        "--from",
        dest="date_from",
        default=None,
        help="Start date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "--to",
        dest="date_to",
        default=None,
        help="End date: YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
    )
    parser.add_argument(
        "-o", "--output", help="Output PNG path (default: ccusage_{period}.png)"
    )
    parser.add_argument(
        "--tz",
        default=None,
        help="Timezone for x-axis and date parsing, e.g. PST, EST, UTC, Asia/Tokyo",
    )
    parser.add_argument(
        "--highlight",
        default=None,
        help="Highlight a daily time window, e.g. 5-11 or 5:00-11:30 (uses --tz)",
    )
    return parser


def _resolve_date_range(args, tz):
    """(start, end, period_label) from the --from/--to/-p/--all combination."""
    now = datetime.now(timezone.utc)
    has_from = args.date_from is not None
    has_to = args.date_to is not None
    has_period = args.period is not None

    if has_from and has_to and has_period:
        print("Error: cannot use --from, --to, and -p together.", file=sys.stderr)
        sys.exit(1)

    if has_from and has_to:
        # Explicit range
        start = parse_datetime(args.date_from, tz)
        end = parse_datetime(args.date_to, tz)
        period_label = f"{args.date_from}_to_{args.date_to}"
    elif has_from and has_period:
        # Start date + period forward
        start = parse_datetime(args.date_from, tz)
        end = start + parse_period(args.period)
        period_label = f"{args.date_from}+{args.period}"
    elif has_from:
        # From date to now
        start = parse_datetime(args.date_from, tz)
        end = now
        period_label = f"{args.date_from}_to_now"
    elif has_to and has_period:
        # Period ending at date
        end = parse_datetime(args.date_to, tz)
        start = end - parse_period(args.period)
        period_label = f"{args.period}_to_{args.date_to}"
    elif has_to:
        print("Error: --to requires either --from or -p.", file=sys.stderr)
        sys.exit(1)
    elif has_period:
        # Period back from now
        delta = parse_period(args.period)
        start = now - delta
        end = now
        period_label = args.period
    elif args.all:
        # All history
        start = None
        end = None
        period_label = "all"
    else:
        # Default: last 24h
        start = now - timedelta(hours=24)
        end = now
        period_label = "24h"
    return start, end, period_label


def main():
    args = _build_parser().parse_args()

    if args.update is not None:
        target = None if args.update is True else args.update
        check_update(target_path=target)
        sys.exit(0)

    tz = resolve_tz(args.tz) if args.tz else None
    start, end, period_label = _resolve_date_range(args, tz)

    print(f"Reading conversation logs from {PROJECTS_DIR} ...", file=sys.stderr)
    events = load_events(start, end)

    if not events:
        print(f"No API calls found for {period_label}.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(events)} API calls for {period_label}.", file=sys.stderr)

    output_path = args.output or f"ccusage_{period_label}.png"
    highlight = parse_highlight(args.highlight) if args.highlight else None

    plot_timeline(events, period_label, output_path, tz=tz, highlight=highlight)


if __name__ == "__main__":
    main()
