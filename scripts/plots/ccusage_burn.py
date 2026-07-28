"""Session burn-rate panel for ccusage_plot.

Split out of ccusage_plot.py to keep every module under pylint's
module-length limit. Everything shared with the main script lives in
ccusage_common; nothing here is imported back by it.
"""

import json
from collections import defaultdict
from datetime import timedelta, timezone
from typing import NamedTuple

import matplotlib.dates as mdates
from matplotlib import ticker
from matplotlib.lines import Line2D

from ccusage_common import (
    BG_AXES,
    BORDER,
    BUCKET_MINUTES,
    BUCKET_THRESHOLD,
    BURN_TOKEN_KEYS,
    BURN_TOKEN_STYLES,
    COLOR_LIMIT_HIT,
    COLOR_WINDOW,
    EMA_ALPHA,
    GRID,
    MODEL_COLORS,
    PROJECTS_DIR,
    SESSION_GAP_S,
    TEXT,
    TEXT_DIM,
    WINDOW_GAP_S,
    human_format,
    parse_event_ts,
    short_model,
)


def build_sessions(events, session_gap_s=SESSION_GAP_S):
    if not events:
        return []
    chunks = []
    cur = [events[0]]
    for e in events[1:]:
        if (e["timestamp"] - cur[-1]["timestamp"]).total_seconds() > session_gap_s:
            chunks.append(cur)
            cur = [e]
        else:
            cur.append(e)
    chunks.append(cur)

    token_keys = ("input", "output", "cache_create", "cache_read")
    field_map = {
        "input": "inputTokens", "output": "outputTokens",
        "cache_create": "cacheCreateTokens", "cache_read": "cacheReadTokens",
    }
    result = []
    for s in chunks:
        if len(s) < 3:
            continue
        dur_h = max((s[-1]["timestamp"] - s[0]["timestamp"]).total_seconds(), 60) / 3600
        per_h = {}
        for key in token_keys:
            per_h[key] = sum(e[field_map[key]] for e in s) / dur_h

        models = defaultdict(int)
        for e in s:
            models[short_model(e["model"])] += 1
        primary = max(models, key=lambda m, ms=models: ms[m])

        result.append({
            "start": s[0]["timestamp"],
            "end": s[-1]["timestamp"],
            "mid": s[0]["timestamp"] + (s[-1]["timestamp"] - s[0]["timestamp"]) / 2,
            "dur_h": dur_h,
            "reqs": len(s),
            "primary_model": primary,
            **{f"{k}_per_h": v for k, v in per_h.items()},
        })
    return result


def find_window_boundaries(events, window_gap_s=WINDOW_GAP_S):
    boundaries = []
    for i in range(1, len(events)):
        gap = (events[i]["timestamp"] - events[i - 1]["timestamp"]).total_seconds()
        if gap >= window_gap_s:
            boundaries.append(events[i]["timestamp"])
    return boundaries


def _limit_hit_from_line(line, seen_uuids):
    """{ts, text} when this JSONL line is a rate-limit error, else None."""
    obj = json.loads(line)
    if obj.get("type") != "assistant" or not obj.get("isApiErrorMessage"):
        return None
    rec_uuid = obj.get("uuid")
    if rec_uuid:
        if rec_uuid in seen_uuids:
            return None
        seen_uuids.add(rec_uuid)
    ts = parse_event_ts(obj.get("timestamp"))
    if ts is None:
        return None
    msg = obj.get("message", {})
    content = msg.get("content", [])
    for c in content if isinstance(content, list) else []:
        if isinstance(c, dict) and c.get("type") == "text":
            t = c.get("text", "").lower()
            if "hit your limit" in t or "rate limit" in t:
                return {"ts": ts, "text": c.get("text", "")}
    return None


def find_limit_hits(events):
    """Scan raw JSONL for rate limit error messages. Uses pre-loaded events' timestamps."""
    limit_hits = []
    # Cross-file dedup by record uuid; see load_events() for rationale.
    seen_uuids: set[str] = set()
    for path in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    hit = _limit_hit_from_line(line, seen_uuids)
                    if hit:
                        limit_hits.append(hit)
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    limit_hits.sort(key=lambda e: e["ts"])
    deduped = []
    for h in limit_hits:
        if not deduped or (h["ts"] - deduped[-1]["ts"]).total_seconds() > 60:
            deduped.append(h)
    return deduped


def _buckets_for_session(session_events, start, end, bucket_s, field_map):
    """Per-bucket token rates for one session's time range."""
    token_keys = ("input", "output", "cache_create", "cache_read")
    buckets = []
    t = start
    while t < end:
        t_end = min(t + timedelta(seconds=bucket_s), end)
        chunk = [e for e in session_events if t <= e["timestamp"] < t_end]
        if not chunk:
            t = t_end
            continue
        dur_h = max((t_end - t).total_seconds(), 60) / 3600
        bucket = {"mid": t + (t_end - t) / 2}
        for key in token_keys:
            bucket[f"{key}_per_h"] = sum(e[field_map[key]] for e in chunk) / dur_h
        buckets.append(bucket)
        t = t_end
    return buckets


def build_buckets(events, sessions, bucket_min=BUCKET_MINUTES):
    field_map = {
        "input": "inputTokens", "output": "outputTokens",
        "cache_create": "cacheCreateTokens", "cache_read": "cacheReadTokens",
    }
    session_ranges = [(s["start"], s["end"]) for s in sessions]
    buckets = []
    for start, end in session_ranges:
        session_events = [e for e in events if start <= e["timestamp"] <= end]
        if len(session_events) < 3:
            continue
        buckets.extend(
            _buckets_for_session(
                session_events, start, end, bucket_min * 60, field_map
            )
        )
    buckets.sort(key=lambda b: b["mid"])
    return buckets


def compute_ema(values, alpha=EMA_ALPHA):
    result = [values[0]]
    for v in values[1:]:
        result.append(alpha * v + (1 - alpha) * result[-1])
    return result


def detect_shifts(ema_values, sessions, lookback=10, threshold=2.0):
    shifts = []
    for i in range(lookback, len(ema_values)):
        baseline = sum(ema_values[i - lookback:i]) / lookback
        if baseline <= 0:
            continue
        ratio = ema_values[i] / baseline
        if ratio >= threshold or ratio <= 1 / threshold:
            shifts.append({
                "ts": sessions[i]["start"],
                "ratio": ratio,
                "direction": "up" if ratio > 1 else "down",
            })
    clustered = []
    for s in shifts:
        if not clustered or (s["ts"] - clustered[-1]["ts"]).total_seconds() > 86400:
            clustered.append(s)
    return clustered


class _BurnData(NamedTuple):
    """Everything the burn panel's draw steps need, precomputed once."""
    timestamps: list
    out_rates: list
    display_emas: dict
    session_emas: dict
    shifts: list
    visible_hits: list
    xlim: tuple
    span_h: float
    rate_mult: float
    rate_unit: str


def _visible_sessions(sessions, view_start, view_end):
    if not view_start and not view_end:
        return sessions
    return [
        s for s in sessions
        if (not view_start or s["end"] >= view_start)
        and (not view_end or s["start"] <= view_end)
    ]


def _burn_emas(sessions, visible, rate_mult):
    """(per-session reference EMAs, shifts, visible display EMAs)."""
    all_emas = {
        key: compute_ema([s[f"{key}_per_h"] for s in sessions])
        for key in BURN_TOKEN_KEYS
    }
    session_emas = {
        key: {id(s): all_emas[key][i] for i, s in enumerate(sessions)}
        for key in BURN_TOKEN_KEYS
    }
    shifts = detect_shifts(all_emas["output"], sessions)
    display_alpha = max(EMA_ALPHA, 2.0 / (len(visible) + 1))
    display_emas = {}
    for key in BURN_TOKEN_KEYS:
        rates = [s[f"{key}_per_h"] for s in visible]
        display_emas[key] = [
            v * rate_mult for v in compute_ema(rates, alpha=display_alpha)
        ]
    return session_emas, shifts, display_emas


def _burn_data(sessions, visible, limit_hits, view_start, view_end):
    xlim_start = view_start or visible[0]["start"] - timedelta(hours=2)
    xlim_end = view_end or visible[-1]["end"] + timedelta(hours=2)
    span_h = (xlim_end - xlim_start).total_seconds() / 3600
    if span_h <= 4:
        rate_mult, rate_unit = 1 / 60, "min"
    else:
        rate_mult, rate_unit = 1, "hour"
    session_emas, shifts, display_emas = _burn_emas(
        sessions, visible, rate_mult
    )
    return _BurnData(
        timestamps=[s["mid"] for s in visible],
        out_rates=[s["output_per_h"] * rate_mult for s in visible],
        display_emas=display_emas,
        session_emas=session_emas,
        shifts=shifts,
        visible_hits=[
            h for h in limit_hits if xlim_start <= h["ts"] <= xlim_end
        ],
        xlim=(xlim_start, xlim_end),
        span_h=span_h,
        rate_mult=rate_mult,
        rate_unit=rate_unit,
    )


def _burn_window_markers(ax, window_boundaries, xlim):
    for wb in window_boundaries:
        if wb < xlim[0] or wb > xlim[1]:
            continue
        ax.axvline(wb, color=COLOR_WINDOW, alpha=0.12, linewidth=1, linestyle=":", zorder=1)


def _burn_bucket_lines(ax, events, visible, rate_mult):
    """Intra-session bucket lines (narrow views)."""
    if len(visible) > BUCKET_THRESHOLD or not events:
        return
    buckets = build_buckets(events, visible)
    if len(buckets) <= len(visible):
        return
    bucket_ts = [b["mid"] for b in buckets]
    for key in BURN_TOKEN_KEYS:
        raw = [b[f"{key}_per_h"] * rate_mult for b in buckets]
        smoothed = compute_ema(raw, alpha=0.3)
        style = BURN_TOKEN_STYLES[key]
        ax.plot(bucket_ts, smoothed, color=style["color"],
                alpha=0.25, linewidth=0.8, zorder=5, linestyle="-")


def _burn_shift_annotations(ax, visible, data):
    visible_shifts = [
        s for s in data.shifts if data.xlim[0] <= s["ts"] <= data.xlim[1]
    ]
    for shift in visible_shifts:
        for s in visible:
            if abs((s["mid"] - shift["ts"]).total_seconds()) >= 7200:
                continue
            y_pos = data.session_emas["output"][id(s)] * data.rate_mult
            if shift["direction"] == "up":
                arrow, fg, bg, edge = "↑", "#ff6666", "#3a1a1a", "#ff6666"
            else:
                arrow, fg, bg, edge = "↓", "#44ff88", "#1a3a2a", "#44ff88"
            ax.annotate(
                f"{arrow} {shift['ratio']:.1f}x",
                xy=(shift["ts"], y_pos),
                xytext=(0, -25), textcoords="offset points",
                fontsize=7, color=fg, ha="center", va="top",
                bbox={"boxstyle": "round,pad=0.2", "facecolor": bg,
                      "edgecolor": edge, "alpha": 0.8},
                zorder=11,
            )
            break


def _burn_draw_marks(ax, events, visible, window_boundaries, data):
    _burn_window_markers(ax, window_boundaries, data.xlim)

    # Session dots
    sizes = [min(max(s["dur_h"] * 60, 25), 250) for s in visible]
    colors = [MODEL_COLORS.get(s["primary_model"], "#888888") for s in visible]
    ax.scatter(data.timestamps, data.out_rates, s=sizes, c=colors, alpha=0.5,
               edgecolors="white", linewidths=0.3, zorder=6)

    # EMA lines
    for key in BURN_TOKEN_KEYS:
        style = BURN_TOKEN_STYLES[key]
        ax.plot(data.timestamps, data.display_emas[key], color=style["color"],
                alpha=style["alpha"], linewidth=style["lw"], zorder=8,
                label=style["label"])

    _burn_bucket_lines(ax, events, visible, data.rate_mult)

    # Rate limit hits
    for hit in data.visible_hits:
        ax.axvline(hit["ts"], color=COLOR_LIMIT_HIT, alpha=0.7, linewidth=2, zorder=9)

    # Behavioral shifts
    _burn_shift_annotations(ax, visible, data)


def _burn_xaxis(ax, span_h):
    fmt_tz = timezone.utc
    if span_h <= 24:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=3))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=fmt_tz))
    elif span_h <= 72:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=6))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d %H:%M", tz=fmt_tz))
    elif span_h <= 168:
        ax.xaxis.set_major_locator(mdates.DayLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
    elif span_h <= 1440:
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d", tz=fmt_tz))
    else:
        ax.xaxis.set_major_locator(mdates.MonthLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y", tz=fmt_tz))


def _burn_style_axes(ax, data):
    all_visible_rates = list(data.out_rates)
    for key in BURN_TOKEN_KEYS:
        all_visible_rates.extend(data.display_emas[key])
    ax.set_yscale("log")
    ax.set_ylim(
        bottom=max(min(all_visible_rates) * 0.3, 1),
        top=max(all_visible_rates) * 3,
    )
    ax.set_xlim(*data.xlim)

    for spine in ax.spines.values():
        spine.set_color(BORDER)
        spine.set_linewidth(1.5)
    ax.tick_params(colors=TEXT_DIM, labelsize=9)
    ax.yaxis.set_major_formatter(ticker.FuncFormatter(
        lambda v, _: human_format(v, False)))
    ax.set_ylabel(f"Tokens / {data.rate_unit} (EMA)", fontsize=11, color=TEXT_DIM)
    ax.grid(True, alpha=0.2, color=GRID, axis="y")
    ax.grid(True, alpha=0.1, color=GRID, axis="x")

    _burn_xaxis(ax, data.span_h)
    ax.tick_params(axis="x", rotation=0, labelsize=8)


def _burn_legend(ax, visible, data):
    legend_handles = []
    for key in BURN_TOKEN_KEYS:
        style = BURN_TOKEN_STYLES[key]
        legend_handles.append(Line2D([0], [0], color=style["color"],
                                     linewidth=style["lw"],
                                     alpha=style["alpha"],
                                     label=f"{style['label']} (EMA)"))
    legend_handles.append(Line2D([0], [0], color=COLOR_WINDOW, alpha=0.3,
                                 linewidth=1, linestyle=":",
                                 label="Window start (5h+ gap)"))
    if data.visible_hits:
        legend_handles.append(Line2D([0], [0], color=COLOR_LIMIT_HIT,
                                     linewidth=2, label="Rate limit hit"))
    for model in sorted(set(s["primary_model"] for s in visible)):
        c = MODEL_COLORS.get(model, "#888888")
        legend_handles.append(Line2D([0], [0], marker="o", color="none",
                                     markerfacecolor=c, markeredgecolor="white",
                                     markeredgewidth=0.3, markersize=8,
                                     alpha=0.6, label=model))
    for dur_label, dur_h in [("30m", 0.5), ("1h", 1), ("4h", 4)]:
        sz = min(max(dur_h * 60, 25), 250)
        legend_handles.append(Line2D([0], [0], marker="o", color="none",
                                     markerfacecolor="#888888",
                                     markeredgecolor="white",
                                     markeredgewidth=0.3,
                                     markersize=sz ** 0.5,
                                     alpha=0.4, label=dur_label))
    ax.legend(handles=legend_handles, loc="lower center",
              bbox_to_anchor=(0.5, 1.03), fontsize=7, ncol=6,
              facecolor=BG_AXES, edgecolor=BORDER, labelcolor=TEXT,
              framealpha=0.9)


def _burn_title(ax, visible, window_boundaries, data):
    t0 = visible[0]["start"].strftime("%b %d")
    t1 = visible[-1]["end"].strftime("%b %d, %Y")
    total_reqs = sum(s["reqs"] for s in visible)
    n_windows = sum(
        1 for wb in window_boundaries if data.xlim[0] <= wb <= data.xlim[1]
    ) + 1
    ax.set_title(
        f"Session Burn Rate  |  {t0} – {t1} UTC"
        f"  |  {len(visible)} sessions, {n_windows} windows, {total_reqs:,} requests",
        fontsize=13, fontweight="bold", color=TEXT, pad=70,
    )


def plot_burn_rate(ax, events, sessions, window_boundaries, limit_hits,
                   view_start=None, view_end=None):
    """Render the session burn rate panel onto the given axes."""
    visible = _visible_sessions(sessions, view_start, view_end)
    if not visible:
        ax.set_visible(False)
        return
    data = _burn_data(sessions, visible, limit_hits, view_start, view_end)
    _burn_draw_marks(ax, events, visible, window_boundaries, data)
    _burn_style_axes(ax, data)
    _burn_legend(ax, visible, data)
    _burn_title(ax, visible, window_boundaries, data)
