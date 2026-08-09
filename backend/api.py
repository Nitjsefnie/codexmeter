"""Read endpoints. All gated by session.auth_middleware via path prefix /api/.

This module holds the shared query helpers (_proj_*, _parse_range,
_bucket_seconds, Phases, _iso) plus the tool/activity/latency/models/
projects/cache endpoints. Two sibling routers split out to keep every
module under the module-length limit:
  - backend/api_dashboard.py: /api/dashboard
  - backend/api_sessions.py:  /api/sessions*, /api/context-growth*
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request
from starlette.responses import StreamingResponse

from backend import db, events, pricing
from backend.cache import cache_response


router = APIRouter(prefix="/api")

log = logging.getLogger("codexmeter.api")

# Per-phase wall-clock for the heavy read endpoints, emitted as one log
# line per request. Gated on CODEXMETER_TIMING so it costs nothing normally,
# but stays in the tree — reconstructing these queries by hand in psql
# drifts from what the endpoint actually runs and hides everything that
# happens outside SQL (row marshalling, response serialisation).
TIMING_ON = os.environ.get("CODEXMETER_TIMING", "").lower() not in ("", "0", "false", "no")

_CODEXMETER_LOGGER = logging.getLogger("codexmeter")

if TIMING_ON and not _CODEXMETER_LOGGER.handlers:
    # uvicorn configures its own loggers and leaves the root logger at
    # WARNING, so a bare log.info() here would go nowhere. Attach our own
    # handler rather than depending on someone else's logging config.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    # Attached to the "codexmeter" PARENT, not "codexmeter.api": ingest logs
    # under "codexmeter.ingest" and was silently discarded, so
    # recompute_canonical / rebuild_* / warm_common reported nothing.
    _CODEXMETER_LOGGER.addHandler(_handler)
    _CODEXMETER_LOGGER.setLevel(logging.INFO)
    _CODEXMETER_LOGGER.propagate = False


class Phases:
    """Collect labelled phase timings and log them as a single line."""

    __slots__ = ("_name", "_marks", "_t0")

    def __init__(self, name: str) -> None:
        self._name = name
        self._marks: list[tuple[str, float]] = []
        self._t0 = time.perf_counter()

    @contextmanager
    def step(self, label: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def mark(self, label: str, seconds: float) -> None:
        self._marks.append((label, seconds))

    def execute(self, label: str, cur, sql: str, args: Any = None):
        """Time a single ``cursor.execute`` and record it under `label`.

        Returns the cursor, so call sites keep their trailing
        ``.fetchall()`` / ``.fetchone()`` unchanged.
        """
        t = time.perf_counter()
        try:
            return cur.execute(sql, args) if args is not None else cur.execute(sql)
        finally:
            self._marks.append((label, time.perf_counter() - t))

    def done(self, **extra: Any) -> None:
        if not TIMING_ON:
            return
        total = (time.perf_counter() - self._t0) * 1000
        parts = " ".join(f"{k}={v * 1000:.0f}ms" for k, v in self._marks)
        tail = " ".join(f"{k}={v}" for k, v in extra.items())
        log.info("TIMING %s total=%.0fms %s %s", self._name, total, parts, tail)


def _known_model(model: str | None) -> bool:
    """Whether a filter names a model the shared pricing catalog knows.

    Tool rollups intentionally have no model dimension, so this validates
    the filter without pretending the surviving calls were attributed by it.
    """
    return bool(model) and pricing.resolve(model).kind == "exact"


UNRESOLVED_PROJECT_ID = "<unresolved>"

# Activity-heatmap timezone. Bound as a SQL parameter (never interpolated);
# Postgres tzdata makes AT TIME ZONE fully DST-aware (CET/CEST transitions).
HEATMAP_TZ = "Europe/Prague"


def _unresolved_cond(prefix: str = "") -> str:
    """SQL boolean: this projects-row's display_name never resolved past the
    raw session hash (no project.json marker in R2 — a bare 12/32-hex id)."""
    return (
        f"{prefix}display_name = {prefix}project_id "
        f"AND {prefix}project_id ~ '^([0-9a-f]{{12}}|[0-9a-f]{{32}})$'"
    )


def _proj_filter(project: str | None, args: list) -> str:
    """SQL snippet filtering `files f` rows by project; appends bind params
    to args. The UNRESOLVED_PROJECT_ID sentinel selects the whole
    unresolved-hash group and binds NO params, so it stays correct in
    queries that interpolate the snippet twice."""
    if not project:
        return ""
    if project == UNRESOLVED_PROJECT_ID:
        return ("AND f.project_id IN (SELECT project_id FROM projects "
                f"WHERE {_unresolved_cond()})")
    args.append(project)
    return "AND f.project_id = %s"


def _proj_semijoin(project: str | None, args: list) -> str:
    """Same filter as `_proj_filter`, expressed as a semi-join on `records`
    with no `files` alias in scope. Queries that select a bare `file_key`
    need this: joining `files` in would make that column reference
    ambiguous."""
    if not project:
        return ""
    if project == UNRESOLVED_PROJECT_ID:
        return (
            "AND file_key IN (SELECT file_key FROM files WHERE project_id IN "
            f"(SELECT project_id FROM projects WHERE {_unresolved_cond()}))"
        )
    args.append(project)
    return "AND file_key IN (SELECT file_key FROM files WHERE project_id = %s)"


def _proj_rollup(project: str | None, args: list) -> str:
    """Same filter again, against `usage_rollup u`, which carries
    project_id directly and so needs no join at all."""
    if not project:
        return ""
    if project == UNRESOLVED_PROJECT_ID:
        return ("AND u.project_id IN (SELECT project_id FROM projects "
                f"WHERE {_unresolved_cond()})")
    args.append(project)
    return "AND u.project_id = %s"


def _tool_source(bucket_s: int) -> str:
    """FROM-clause for the tool endpoints, aliased `t` either way.

    `tool_rollup` is hourly, so it can only express display buckets at
    least an hour wide; the 24h view buckets at 5 minutes and falls back
    to a live per-call subquery shaped with the SAME column names, which
    is what lets both endpoints be written once.
    """
    if bucket_s >= 3600:
        return "tool_rollup t"
    return """(
      SELECT tu.ts AS hour, f.project_id, tu.tool_name,
             1::bigint AS n_total,
             (CASE WHEN tu.is_error IS NOT NULL THEN 1 ELSE 0 END)::bigint AS n_rated,
             (CASE WHEN tu.is_error THEN 1 ELSE 0 END)::bigint            AS n_error,
             tu.lines_added, tu.lines_deleted
        FROM tool_uses tu
        JOIN files f ON f.file_key = tu.file_key
       WHERE tu.ts IS NOT NULL
    ) t"""


def _proj_tool(project: str | None, args: list) -> str:
    """Project filter against the tool source, which carries project_id."""
    if not project:
        return ""
    if project == UNRESOLVED_PROJECT_ID:
        return ("AND t.project_id IN (SELECT project_id FROM projects "
                f"WHERE {_unresolved_cond()})")
    args.append(project)
    return "AND t.project_id = %s"


@router.get("/me")
def me(request: Request) -> dict:
    """Identity probe — frontend uses `is_guest` to decide which UI
    affordances to render."""
    return {
        "user_id": getattr(request.state, "user_id", None),
        "is_guest": bool(getattr(request.state, "is_guest", False)),
    }


@router.get("/tool-usage")
@cache_response
def tool_usage(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed tool-call counts. Bucket size = largest in [60s, 1d]
    that yields ≥100 bins across the range. Frontend stacks to 100%
    and promotes any tool that ever cracked top-N at any bucket.
    Tools that never make the cut land in 'Other'.

    Tool rollups have no model dimension. A known exact catalog model keeps
    the shared tool data available; an unknown model returns no buckets.
    The response must not be interpreted as model-attributed tool usage."""
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # Every record carries a model, but tool_uses deliberately does not.
    # Joining `records` to filter by model is BROKEN here because
    # tool_uses.line_num != records.line_num in Kimi wire.jsonl (tool_uses
    # live on ToolCall lines, records on StatusUpdate lines — disjoint sets).
    # Apply a catalog gate in Python: known model means the shared tool data
    # remains available; unknown model means an empty result.
    if model and not _known_model(model):
        return {"range": range_, "project": project, "bucket_s": bucket_s, "buckets": []}
    args: list[Any] = [since]
    proj_filter = _proj_tool(project, args)

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_literal(f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM t.hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   t.tool_name    AS tool,
                   SUM(t.n_total) AS n
            FROM {_tool_source(bucket_s)}
            WHERE t.hour >= %s {proj_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            args,
        ).fetchall()

    return {
        "range": range_,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "tool": t, "n": int(n or 0)}
            for (b, t, n) in rows
        ],
    }


@router.get("/tool-error-rate")
@cache_response
def tool_error_rate(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed (n_total, n_error) per (model, tool_name) over settled
    tool calls only (is_error IS NOT NULL). The frontend computes
    error-rate = n_error / n_total per series and EMA-smooths the
    sequence.

    Tool rollups have no model dimension. A known exact catalog model keeps
    the shared tool data available; an unknown model returns no buckets.
    Cross-file uuid dedup does NOT apply — tool_uses aren't keyed on
    records.uuid; the natural boundary is per-file."""
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # See tool_usage above for why the records JOIN is wrong for tool data
    # (tool_uses.line_num lives on ToolCall lines, records.line_num on
    # StatusUpdate lines — they're disjoint, so the JOIN produces zero rows
    # and the frontend sees an empty result). Validate through the shared
    # pricing catalog instead of restating parser labels here.
    if model and not _known_model(model):
        return {"range": range_, "project": project, "bucket_s": bucket_s, "buckets": []}
    args: list[Any] = [since]
    proj_filter = _proj_tool(project, args)

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_literal(f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM t.hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   %s          AS model,
                   t.tool_name AS tool,
                   SUM(t.n_rated) AS n_total,
                   SUM(t.n_error) AS n_error
            FROM {_tool_source(bucket_s)}
            WHERE t.hour >= %s {proj_filter}
            GROUP BY 1, 3
            HAVING SUM(t.n_rated) > 0
            ORDER BY 1, 3
            """),
            [model or "unknown", *args],
        ).fetchall()

    return {
        "range": range_,
        "project": project,
        "bucket_s": bucket_s,
        "buckets": [
            {"ts": _iso(b), "model": m, "tool": t,
             "n_total": int(nt or 0), "n_error": int(ne or 0)}
            for (b, m, t, nt, ne) in rows
        ],
    }


@router.get("/activity-heatmap")
@cache_response
def activity_heatmap(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Weekday × hour activity grid in HEATMAP_TZ local wall-clock time.

    dow is ISO (1=Mon … 7=Sun), hour 0–23. DST handled by Postgres
    tzdata via AT TIME ZONE — UTC+1 in winter (CET), UTC+2 in summer
    (CEST).

    Served from usage_rollup: the grid is weekday x hour of pure
    sums/counts, which is exactly what the rollup holds, and its `hour`
    column is already dedup-resolved. Truncating to the hour in UTC is
    safe for this because HEATMAP_TZ's offsets are whole hours, so the
    local hour bucket is preserved. There is no bucket-width gate here
    (unlike /api/dashboard) — the grid is always hourly."""
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta

    args: list[Any] = [HEATMAP_TZ, HEATMAP_TZ, since]
    proj_filter = _proj_rollup(project, args)
    model_filter = "AND u.model LIKE %s" if model else ""
    if model:
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_literal(f"""
            SELECT EXTRACT(ISODOW FROM (u.hour AT TIME ZONE %s))::int AS dow,
                   EXTRACT(HOUR   FROM (u.hour AT TIME ZONE %s))::int AS hour,
                   SUM(u.requests)      AS requests,
                   SUM(u.output_tokens) AS output_tokens,
                   SUM(u.cost_usd)      AS cost_usd
            FROM usage_rollup u
            WHERE u.hour >= date_trunc('hour', %s::timestamptz)
              {proj_filter} {model_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            args,
        ).fetchall()

    return {
        "range": range_,
        "tz": HEATMAP_TZ,
        "cells": [
            {
                "dow": int(dow),
                "hour": int(hour),
                "requests": int(n or 0),
                "output_tokens": int(out or 0),
                "cost_usd": float(cost or 0),
            }
            for (dow, hour, n, out, cost) in rows
        ],
    }


# Bucket widths latency_rollup is built for (ingest.LATENCY_BUCKETS).
_LATENCY_ROLLUP_BUCKETS = (3600, 21600, 43200, 86400)


def _latency_rollup_marshal(rows) -> tuple[list, list]:
    """(bands, outliers) response lists from latency_rollup rows."""
    bands, outliers = [], []
    for (b, m, n, p10, p50, p90, dots) in rows:
        bands.append({
            "ts": _iso(b), "model": m, "n": int(n or 0),
            "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
        })
        for d in (dots or []):
            outliers.append({
                "ts": d.get("ts"),
                "model": m,
                "latency_s": float(d.get("latency_s") or 0),
                "file_key": d.get("file_key"),
                "line": int(d.get("line_num") or 0),
            })
    return bands, outliers


def _latency_rollup_rows(bucket_s: int, since, project, model):
    """(bands, outliers) from `latency_rollup`, or None to use the live path.

    The rollup cannot answer two cases:
      * sub-hour display buckets (the 24h view) are not built — a row per
        five minutes of all history to serve one day is not worth storing.
      * the <unresolved> sentinel selects a GROUP of projects, and a
        percentile over that group is not derivable from the per-project
        rows the table holds. Only a single project (or no filter, which
        is the stored project_id = '' row) can be served.
    """
    if bucket_s not in _LATENCY_ROLLUP_BUCKETS:
        return None
    if project == UNRESOLVED_PROJECT_ID:
        return None

    args: list[Any] = [bucket_s, project or "", since]
    model_filter = ""
    if model:
        model_filter = "AND model LIKE %s"
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            db.sql_literal(f"""
            SELECT bucket, model, n, p10, p50, p90, outliers
            FROM latency_rollup
            WHERE bucket_s = %s AND project_id = %s AND bucket >= %s
              {model_filter}
            ORDER BY bucket, model
            """),
            args,
        ).fetchall()

    return _latency_rollup_marshal(rows)


def _latency_live_sql(bucket_s: int, proj_filter: str,
                      model_filter: str) -> tuple[str, str]:
    """(bands_sql, outliers_sql) for the live (non-rollup) path."""
    # Bands: per-(bucket, model) percentiles.
    bands_sql = f"""
    SELECT to_timestamp(
             floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
           ) AS bucket,
           COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
           COUNT(*) AS n,
           PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p10,
           PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p50,
           PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY r.reply_latency_s) AS p90
    FROM records r
    JOIN files f ON f.file_key = r.file_key
    WHERE r.ts >= %s {proj_filter} {model_filter}
      AND r.reply_latency_s IS NOT NULL
    GROUP BY 1, 2
    ORDER BY 1, 2
    """

    # Outliers: top 1% slowest + bottom 1% fastest per (bucket, model)
    # bucket. Skip buckets with n < 100 — 1% of <100 is <1, so the
    # min/max would dominate and pollute the panel.
    outliers_sql = f"""
    WITH ranked AS (
      SELECT to_timestamp(
               floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
             ) AS bucket,
             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
             r.ts                AS event_ts,
             r.file_key,
             r.line_num,
             r.reply_latency_s AS latency_s,
             COUNT(*) OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
             ) AS bucket_n,
             ROW_NUMBER() OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
               ORDER BY r.reply_latency_s DESC
             ) AS rn_high,
             ROW_NUMBER() OVER (PARTITION BY
               to_timestamp(floor(EXTRACT(EPOCH FROM r.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2),
               COALESCE(NULLIF(r.model, ''), 'unknown')
               ORDER BY r.reply_latency_s ASC
             ) AS rn_low
      FROM records r
      JOIN files f ON f.file_key = r.file_key
      WHERE r.ts >= %s {proj_filter} {model_filter}
        AND r.reply_latency_s IS NOT NULL
    )
    SELECT bucket, model, event_ts, file_key, line_num, latency_s
    FROM ranked
    WHERE bucket_n >= 100
      AND (rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
        OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01)))
    ORDER BY bucket, model, latency_s DESC
    """
    return bands_sql, outliers_sql


def _latency_bands(bands_rows) -> list:
    return [
        {
            "ts": _iso(b), "model": m, "n": int(n or 0),
            "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
        }
        for (b, m, n, p10, p50, p90) in bands_rows
    ]


def _latency_outliers(outlier_rows) -> list:
    return [
        {
            "ts": _iso(et), "model": m,
            "latency_s": float(lat or 0),
            "file_key": fk, "line": int(ln or 0),
        }
        for (b, m, et, fk, ln, lat) in outlier_rows
    ]


def _latency_live_rows(bucket_s: int, proj_filter: str, model_filter: str,
                       args: list[Any]) -> tuple[list, list]:
    """(bands_rows, outlier_rows) from the live (non-rollup) queries."""
    bands_sql, outliers_sql = _latency_live_sql(
        bucket_s, proj_filter, model_filter
    )
    with db.viz_conn() as c:
        bands_rows = c.execute(db.sql_literal(bands_sql), args).fetchall()
        outlier_rows = c.execute(db.sql_literal(outliers_sql), args).fetchall()
    return bands_rows, outlier_rows


@router.get("/reply-latency")
@cache_response
def reply_latency(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Per-(bucket, model) reply-latency percentiles + per-bucket
    top/bottom 1% outliers. Latency is the gap from each anchored user
    message to its assistant reply, computed at parse time
    (records.reply_latency_s). Model & project filters apply to the
    assistant record's model/project.

    Percentiles do not compose, so this cannot sum a fine-grained rollup
    the way /api/dashboard does. It reads `latency_rollup` instead, which
    stores the bands already computed per display bucket width — the
    buckets are epoch-aligned, so a range filter just selects which rows
    to return, exactly rather than approximately. See _latency_rollup_rows
    for when that path does not apply."""
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)

    rolled = _latency_rollup_rows(bucket_s, since, project, model)
    if rolled is not None:
        bands, outliers = rolled
        return {
            "range": range_,
            "project": project,
            "model": model,
            "bucket_s": bucket_s,
            "bands": bands,
            "outliers": outliers,
        }

    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")

    bands_rows, outlier_rows = _latency_live_rows(
        bucket_s, proj_filter, model_filter, args
    )

    return {
        "range": range_,
        "project": project,
        "model": model,
        "bucket_s": bucket_s,
        "bands": _latency_bands(bands_rows),
        "outliers": _latency_outliers(outlier_rows),
    }


@router.get("/events")
async def event_stream(request: Request):
    """Server-Sent Events stream. Currently emits one event:
      event: ingest_done
      data: {...summary...}
    The frontend reacts by re-fetching /api/dashboard. A 15-second
    heartbeat (':' comment line) keeps the connection alive through
    Cloudflare and other intermediaries."""

    async def gen():
        q = events.subscribe()
        shutdown = events.shutdown_event()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if shutdown is not None and shutdown.is_set():
                    break
                # Race the queue, the shutdown signal, and a 15s heartbeat.
                # First-wins; everything else is cancelled.
                wait_tasks = [asyncio.create_task(q.get())]
                if shutdown is not None:
                    wait_tasks.append(asyncio.create_task(shutdown.wait()))
                done, pending = await asyncio.wait(
                    wait_tasks,
                    timeout=15,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if not done:
                    yield ": ping\n\n"
                    continue
                if shutdown is not None and shutdown.is_set():
                    break
                # Queue task finished — drain it
                first = done.pop()
                try:
                    payload = first.result()
                    yield payload
                except asyncio.CancelledError:
                    break
        finally:
            events.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models")
def list_models() -> dict:
    """All distinct (real, non-synthetic) model strings ever recorded,
    with counts. Frontend canonicalizes via shortModelName for the
    dropdown."""
    with db.viz_conn() as c:
        rows = c.execute(
            """
            SELECT model, COUNT(*) AS n
            FROM records
            WHERE model <> ''
            GROUP BY model
            ORDER BY 2 DESC
            """
        ).fetchall()
    return {"models": [{"model": m, "n": int(n)} for (m, n) in rows]}


@router.get("/projects")
@cache_response
def list_projects(range_: str = Query("30d", alias="range")) -> dict:
    """Per-project rollup: session_count, range-scoped cost, derived from
    files+usage_rollup. Ordered by the RANGE-scoped cost, descending, so
    the picker re-sorts as the dashboard's time range changes — the same
    `range` convention /api/dashboard takes (`_parse_range`, default
    "30d").

    Projects whose ALL-TIME cost is 0 are dropped entirely (never-cost
    projects are noise — 334 of 496 codexmeter projects, expected). A
    project with all-time cost but nothing in the selected range is still
    returned — sorted to the bottom with a cost of 0 — since this is a
    re-sort of the existing list, not a range filter; the ALL-TIME-zero
    exclusion and the RANGE-scoped ordering are two different aggregates
    and must not be conflated. Applies to the aggregated `<unresolved>`
    bucket as a single row, same as every other project.

    Cost comes from usage_rollup instead of joining every record: this used
    to fan `projects x files x records` out to a row per record, and was
    the slowest uncached call on a page load after /api/dashboard.

    The aggregates are computed in separate subqueries rather than by
    stacking two LEFT JOINs — joining files AND records first multiplied
    the file rows by their record count, so COUNT(f.file_key) reported a
    file total inflated by the average records-per-file.
    """
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    with db.viz_conn() as c:
        cond = _unresolved_cond("p.")
        rows = c.execute(
            db.sql_literal(f"""
            SELECT CASE WHEN {cond} THEN '<unresolved>'
                        ELSE p.project_id END   AS project_id,
                   CASE WHEN {cond} THEN '<unresolved>'
                        ELSE p.display_name END AS display_name,
                   COALESCE(SUM(fc.session_count), 0) AS session_count,
                   COALESCE(SUM(rc.range_cost), 0)    AS range_cost
            FROM projects p
            LEFT JOIN (
              SELECT project_id,
                     COUNT(DISTINCT session_id) AS session_count
              FROM files GROUP BY project_id
            ) fc ON fc.project_id = p.project_id
            LEFT JOIN (
              SELECT project_id, SUM(cost_usd) AS total_cost
              FROM usage_rollup GROUP BY project_id
            ) uc ON uc.project_id = p.project_id
            LEFT JOIN (
              SELECT project_id, SUM(cost_usd) AS range_cost
              FROM usage_rollup
              WHERE hour >= date_trunc('hour', %s::timestamptz)
              GROUP BY project_id
            ) rc ON rc.project_id = p.project_id
            GROUP BY 1, 2
            HAVING COALESCE(SUM(uc.total_cost), 0) <> 0
            ORDER BY range_cost DESC
            """),
            (since,),
        ).fetchall()
    return {
        "projects": [
            {
                "project_id": pid,
                "display_name": name,
                "session_count": int(sessions),
                "total_cost": float(cost),
            }
            for pid, name, sessions, cost in rows
        ],
    }


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


_BUCKET_CANDIDATES_S = (60, 5*60, 15*60, 30*60, 3600, 6*3600, 12*3600, 86400)


def _bucket_seconds(delta: timedelta) -> int:
    """Pick the LARGEST bucket size in [60s, 86400s] (≤ 1 day) that
    still produces ≥100 bins across the range. Mirrors the frontend's
    dashboard binMs picker; applied to every server-side bucketed
    query so 24h ranges don't get hardcoded-hourly 24 buckets."""
    span_s = max(1, int(delta.total_seconds()))
    chosen = _BUCKET_CANDIDATES_S[0]
    for b in _BUCKET_CANDIDATES_S:
        if b > 86400:
            break
        if span_s / b < 100:
            break
        chosen = b
    return chosen


def _parse_range(s: str) -> timedelta:
    """`Nd` / `Nh` parse normally. `all` returns now-epoch so callers
    that compute `since = now - delta` end up at the unix epoch — i.e.
    every row in the DB, not an arbitrary 100-year window."""
    if s == "all":
        return datetime.now(timezone.utc) - _EPOCH
    if s.endswith("d"):
        return timedelta(days=int(s[:-1]))
    if s.endswith("h"):
        return timedelta(hours=int(s[:-1]))
    raise HTTPException(400, f"bad range: {s!r}")


def _iso(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def _per_model_row(row) -> dict:
    model, turns, fresh, cr, output, cost = row
    fresh = int(fresh or 0)
    cr = int(cr or 0)
    output = int(output or 0)
    rates = pricing.rate_for(model)
    f_cost = fresh * rates["fresh"] / 1_000_000
    rd_cost = cr * rates["read"] / 1_000_000
    o_cost = output * rates["output"] / 1_000_000
    total_in = fresh + cr
    return {
        "model": model,
        "turns": int(turns or 0),
        "fresh": fresh,
        "cache_read": cr,
        "output": output,
        "estimated_rate": pricing.resolve(model).estimated,
        "hit_rate_pct": round((cr / total_in * 100.0) if total_in else 0.0, 1),
        "cost_total": round(float(cost or 0), 4),
        "cost_buckets": {
            "fresh": round(f_cost, 4),
            "read": round(rd_cost, 4),
            "output": round(o_cost, 4),
        },
    }


def _session_total(per_model: list) -> dict:
    total = {
        "turns": sum(m["turns"] for m in per_model),
        "fresh": sum(m["fresh"] for m in per_model),
        "cache_read": sum(m["cache_read"] for m in per_model),
        "output": sum(m["output"] for m in per_model),
        "cost_total": round(sum(m["cost_total"] for m in per_model), 4),
        "cost_buckets": {
            k: round(sum(m["cost_buckets"][k] for m in per_model), 4)
            for k in ("fresh", "read", "output")
        },
        "estimated_rate": any(m["estimated_rate"] for m in per_model),
    }
    total_in = total["fresh"] + total["cache_read"]
    total["hit_rate_pct"] = round(
        (total["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
    )
    return total


def _top_rows(rows, columns) -> list:
    out = []
    for row in rows:
        d = {}
        for col, v in zip(columns, row):
            if hasattr(v, "isoformat"):
                d[col] = v.isoformat()
            elif col == "cost":
                d[col] = float(v) if v is not None else 0.0
            elif col in ("ts", "model", "file_key"):
                d[col] = v
            else:
                d[col] = int(v or 0)
        out.append(d)
    return out


@router.get("/cache")
@cache_response
def cache_view(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Literal replica of parse_wire.py --cache output.

    Returns:
      {
        range, project,
        per_model: [{model, turns, fresh, cache_read, output, estimated_rate,
                     hit_rate_pct, cost_total, cost_buckets}],
        session_total: {same shape, summed across per_model},
        top_output: [{ts, line, model, output, c_read, fresh, cost, file_key}],
        top_cache_read: [...]
      }

    Cross-file uuid dedup comes from records.is_canonical, resolved at
    ingest (SV-CANONICAL-FLAG). Records with NULL uuid (legacy) are always
    canonical, matching the old UNION ALL leg that kept them verbatim.

    Kimi wire format never emits input_cache_creation > 0, so cache_create /
    create buckets are dropped from the response entirely.
    """
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    model_filter = ""
    canon_args: list[Any] = [since]
    proj_filter = _proj_semijoin(project, canon_args)
    if model:
        model_filter = "AND model LIKE %s"
        canon_args.append(f"%{model}%")

    ph = Phases("cache_view")

    # Dedup used to be a CTE prefixed onto each of the three queries below,
    # so Postgres re-ran the whole DISTINCT ON — the single most expensive
    # step — once per query. It is now resolved at ingest into
    # records.is_canonical (ingest.recompute_canonical), so each query just
    # filters a boolean.
    canon_src = f"""
        FROM records
        WHERE ts >= %s AND is_canonical {proj_filter} {model_filter}
    """

    with db.viz_conn() as c:
        per_model_rows = ph.execute(
            "per_model", c,
            db.sql_literal(f"""
            SELECT model,
                   COUNT(*)                    AS turns,
                   SUM(fresh_tokens)           AS fresh,
                   SUM(cache_read_tokens)      AS cache_read,
                   SUM(output_tokens)          AS output,
                   SUM(cost_usd)               AS cost_total
            {canon_src}
            GROUP BY model
            ORDER BY cost_total DESC
            """),
            canon_args,
        ).fetchall()

        top_output = ph.execute(
            "top_output", c,
            db.sql_literal(f"""
            SELECT ts, line_num, model,
                   output_tokens, cache_read_tokens,
                   fresh_tokens, cost_usd, file_key
            {canon_src}
            ORDER BY output_tokens DESC
            LIMIT 10
            """),
            canon_args,
        ).fetchall()

        top_read = ph.execute(
            "top_read", c,
            db.sql_literal(f"""
            SELECT ts, line_num, model,
                   cache_read_tokens,
                   output_tokens, fresh_tokens,
                   cost_usd, file_key
            {canon_src}
              AND cache_read_tokens > 0
            ORDER BY cache_read_tokens DESC
            LIMIT 10
            """),
            canon_args,
        ).fetchall()

    ph.done(range=range_, project=project, model=model)

    per_model = [_per_model_row(r) for r in per_model_rows]

    return {
        "range": range_,
        "project": project,
        "per_model": per_model,
        "session_total": _session_total(per_model),
        "top_output": _top_rows(top_output, [
            "ts", "line", "model",
            "output", "c_read", "fresh",
            "cost", "file_key",
        ]),
        "top_cache_read": _top_rows(top_read, [
            "ts", "line", "model",
            "c_read", "output", "fresh", "cost", "file_key",
        ]),
    }
