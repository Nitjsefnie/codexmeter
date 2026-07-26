"""Read endpoints. All gated by session.auth_middleware via path prefix /api/.

Per-FILE / per-RECORD shape (R1+R2+R3+R4):
  - /api/projects: list of projects with file_count + total_cost
  - /api/cache: literal compute_cache replica (per-model + top10 + buckets)
  - /api/sessions/{id}/transcript: raw bytes for Inspector (LRU cache)
  - /api/sessions/{id}/sidecar: path-validated sidecar fetch

Legacy compatibility shims (R11) for the restored Dashboard / SessionsList /
SessionView frontend (post-revert of R9). Sourced from new files+records
tables but returning OLD response shape:
  - /api/dashboard:        hourly aggregates + burns + ctx_lines
  - /api/sessions:         paginated session list
  - /api/sessions/{id}:    single session detail
"""
from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from backend import cache, db, pricing, r2
from backend.cache import cache_response


router = APIRouter(prefix="/api")

log = logging.getLogger("kimimeter.api")

# Per-phase wall-clock for the heavy read endpoints, emitted as one log
# line per request. Gated on KIMIMETER_TIMING so it costs nothing normally,
# but stays in the tree — reconstructing these queries by hand in psql
# drifts from what the endpoint actually runs and hides everything that
# happens outside SQL (row marshalling, response serialisation).
TIMING_ON = os.environ.get("KIMIMETER_TIMING", "").lower() not in ("", "0", "false", "no")

if TIMING_ON and not log.handlers:
    # uvicorn configures its own loggers and leaves the root logger at
    # WARNING, so a bare log.info() here would go nowhere. Attach our own
    # handler rather than depending on someone else's logging config.
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False


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


# Kimi-only ingest right now — parse.py emits one of these for every record.
# When kimimeter starts ingesting other ecosystems (Claude jsonls, etc.)
# the JOIN-by-line_num assumption breaks for those sources too; at that
# point promote model to a `tool_uses.model` column populated at parse time.
_ONLY_MODELS = ("kimi-k2-6", "kimi-k2-7-code", "kimi-k3")

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
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed tool-call counts. Bucket size = largest in [60s, 1d]
    that yields ≥100 bins across the range. Frontend stacks to 100%
    and promotes any tool that ever cracked top-N at any bucket.
    Tools that never make the cut land in 'Other'.

    `model=opus-4-7` filters to tool calls emitted by an assistant
    message whose record matches the model substring (joined on
    file_key + line_num)."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # Model filter: in Kimi, every record carries model='kimi-k2-6' or
    # 'kimi-k2-7-code' (assigned by first-event timestamp in parse.py).
    # Joining `records` to filter by model is BROKEN here because
    # tool_uses.line_num != records.line_num in Kimi wire.jsonl (tool_uses
    # live on ToolCall lines, records on StatusUpdate lines — disjoint sets).
    # Apply the model filter in Python: if the requested substring matches a
    # model we ingest, pass; else short-circuit to an empty result.
    if model and not any(m in model for m in _ONLY_MODELS):
        return {"range": range, "project": project, "bucket_s": bucket_s, "buckets": []}
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   tu.tool_name AS tool,
                   COUNT(*)     AS n
            FROM tool_uses tu
            JOIN files f ON f.file_key = tu.file_key
            WHERE tu.ts >= %s {proj_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            args,
        ).fetchall()

    return {
        "range": range,
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
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Bucketed (n_total, n_error) per (model, tool_name) over settled
    tool calls only (is_error IS NOT NULL). The frontend computes
    error-rate = n_error / n_total per series and EMA-smooths the
    sequence.

    `model` is an optional model substring filter (parity with
    /api/tool-usage). Cross-file uuid dedup does NOT apply — tool_uses
    aren't keyed on records.uuid; the natural boundary is per-file."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    # See tool_usage above for why the records JOIN is wrong for Kimi data
    # (tool_uses.line_num lives on ToolCall lines, records.line_num on
    # StatusUpdate lines — they're disjoint, so the JOIN produces zero rows
    # and the frontend sees an empty result). Hardcode the models the parser
    # emits and apply the filter in Python.
    if model and not any(m in model for m in _ONLY_MODELS):
        return {"range": range, "project": project, "bucket_s": bucket_s, "buckets": []}
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM tu.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   %s           AS model,
                   tu.tool_name AS tool,
                   COUNT(*)                              AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error)   AS n_error
            FROM tool_uses tu
            JOIN files   f ON f.file_key = tu.file_key
            WHERE tu.is_error IS NOT NULL
              AND tu.ts >= %s
              {proj_filter}
            GROUP BY 1, 3
            ORDER BY 1, 3
            """,
            [model if model else _ONLY_MODELS[0], *args],
        ).fetchall()

    return {
        "range": range,
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
    range: str = Query("30d"),
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
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta

    args: list[Any] = [HEATMAP_TZ, HEATMAP_TZ, since]
    proj_filter = _proj_rollup(project, args)
    model_filter = "AND u.model LIKE %s" if model else ""
    if model:
        args.append(f"%{model}%")

    with db.viz_conn() as c:
        rows = c.execute(
            f"""
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
            """,
            args,
        ).fetchall()

    return {
        "range": range,
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


@router.get("/reply-latency")
@cache_response
def reply_latency(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Per-(bucket, model) reply-latency percentiles + per-bucket
    top/bottom 1% outliers. Latency is the gap from each anchored user
    message to its assistant reply, computed at parse time
    (records.reply_latency_s). Model & project filters apply to the
    assistant record's model/project."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    args: list[Any] = []
    if model:
        # JOIN happens at the records level via the WHERE clause; no
        # separate join arg needed since records IS the source.
        pass
    args.append(since)
    proj_filter = _proj_filter(project, args)
    model_filter = ""
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")

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

    args2 = list(args) + list(args)  # bands + outliers each take the full arg set

    with db.viz_conn() as c:
        bands_rows = c.execute(bands_sql, args).fetchall()
        outlier_rows = c.execute(outliers_sql, args).fetchall()
    _ = args2  # kept for symmetry; both queries use `args` independently

    return {
        "range": range,
        "project": project,
        "model": model,
        "bucket_s": bucket_s,
        "bands": [
            {
                "ts": _iso(b), "model": m, "n": int(n or 0),
                "p10": float(p10 or 0), "p50": float(p50 or 0), "p90": float(p90 or 0),
            }
            for (b, m, n, p10, p50, p90) in bands_rows
        ],
        "outliers": [
            {
                "ts": _iso(et), "model": m,
                "latency_s": float(lat or 0),
                "file_key": fk, "line": int(ln or 0),
            }
            for (b, m, et, fk, ln, lat) in outlier_rows
        ],
    }


@router.get("/events")
async def event_stream(request: Request):
    """Server-Sent Events stream. Currently emits one event:
      event: ingest_done
      data: {...summary...}
    The frontend reacts by re-fetching /api/dashboard. A 15-second
    heartbeat (':' comment line) keeps the connection alive through
    Cloudflare and other intermediaries."""
    import asyncio as _asyncio
    from backend import events as _events

    async def gen():
        q = _events.subscribe()
        shutdown = _events.shutdown_event()
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                if shutdown is not None and shutdown.is_set():
                    break
                # Race the queue, the shutdown signal, and a 15s heartbeat.
                # First-wins; everything else is cancelled.
                wait_tasks = [_asyncio.create_task(q.get())]
                if shutdown is not None:
                    wait_tasks.append(_asyncio.create_task(shutdown.wait()))
                done, pending = await _asyncio.wait(
                    wait_tasks,
                    timeout=15,
                    return_when=_asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if not done:
                    yield ": ping\n\n"
                    continue
                if shutdown is not None and shutdown.is_set():
                    break
                # Queue task finished — drain it
                first = next(iter(done))
                try:
                    payload = first.result()
                    yield payload
                except _asyncio.CancelledError:
                    break
        finally:
            _events.unsubscribe(q)

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
def list_projects() -> dict:
    """Per-project rollup: file_count, total_cost, derived from files+records.

    Cost comes from usage_rollup instead of joining every record: this used
    to fan `projects x files x records` out to a row per record, and was
    the slowest uncached call on a page load after /api/dashboard.

    The aggregates are computed in separate subqueries rather than by
    stacking two LEFT JOINs — joining files AND records first multiplied
    the file rows by their record count, so COUNT(f.file_key) reported a
    file total inflated by the average records-per-file.
    """
    with db.viz_conn() as c:
        cond = _unresolved_cond("p.")
        rows = c.execute(
            f"""
            SELECT CASE WHEN {cond} THEN '<unresolved>'
                        ELSE p.project_id END   AS project_id,
                   CASE WHEN {cond} THEN '<unresolved>'
                        ELSE p.display_name END AS display_name,
                   COALESCE(SUM(fc.session_count), 0) AS session_count,
                   COALESCE(SUM(fc.file_count), 0)    AS file_count,
                   COALESCE(SUM(uc.total_cost), 0)    AS total_cost
            FROM projects p
            LEFT JOIN (
              SELECT project_id,
                     COUNT(DISTINCT session_id) AS session_count,
                     COUNT(*)                   AS file_count
              FROM files GROUP BY project_id
            ) fc ON fc.project_id = p.project_id
            LEFT JOIN (
              SELECT project_id, SUM(cost_usd) AS total_cost
              FROM usage_rollup GROUP BY project_id
            ) uc ON uc.project_id = p.project_id
            GROUP BY 1, 2
            ORDER BY total_cost DESC
            """
        ).fetchall()
    return {
        "projects": [
            {
                "project_id": pid,
                "display_name": name,
                "session_count": int(sessions),
                "file_count": int(files),
                "total_cost": float(cost),
            }
            for pid, name, sessions, files, cost in rows
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


@router.get("/cache")
@cache_response
def cache_view(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
) -> dict:
    """Literal replica of parse_wire.py --cache output.

    Returns:
      {
        range, project,
        per_model: [{model, turns, fresh, cache_read, output,
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
    delta = _parse_range(range)
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
            f"""
            SELECT model,
                   COUNT(*)                    AS turns,
                   SUM(fresh_tokens)           AS fresh,
                   SUM(cache_read_tokens)      AS cache_read,
                   SUM(output_tokens)          AS output,
                   SUM(cost_usd)               AS cost_total
            {canon_src}
            GROUP BY model
            ORDER BY cost_total DESC
            """,
            canon_args,
        ).fetchall()

        top_output = ph.execute(
            "top_output", c,
            f"""
            SELECT ts, line_num, model,
                   output_tokens, cache_read_tokens,
                   fresh_tokens, cost_usd, file_key
            {canon_src}
            ORDER BY output_tokens DESC
            LIMIT 10
            """,
            canon_args,
        ).fetchall()

        top_read = ph.execute(
            "top_read", c,
            f"""
            SELECT ts, line_num, model,
                   cache_read_tokens,
                   output_tokens, fresh_tokens,
                   cost_usd, file_key
            {canon_src}
              AND cache_read_tokens > 0
            ORDER BY cache_read_tokens DESC
            LIMIT 10
            """,
            canon_args,
        ).fetchall()

    def _per_model(row):
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
            "hit_rate_pct": round((cr / total_in * 100.0) if total_in else 0.0, 1),
            "cost_total": round(float(cost or 0), 4),
            "cost_buckets": {
                "fresh": round(f_cost, 4),
                "read": round(rd_cost, 4),
                "output": round(o_cost, 4),
            },
        }

    ph.done(range=range, project=project, model=model)

    per_model = [_per_model(r) for r in per_model_rows]

    session_total = {
        "turns": sum(m["turns"] for m in per_model),
        "fresh": sum(m["fresh"] for m in per_model),
        "cache_read": sum(m["cache_read"] for m in per_model),
        "output": sum(m["output"] for m in per_model),
        "cost_total": round(sum(m["cost_total"] for m in per_model), 4),
        "cost_buckets": {
            k: round(sum(m["cost_buckets"][k] for m in per_model), 4)
            for k in ("fresh", "read", "output")
        },
    }
    total_in = session_total["fresh"] + session_total["cache_read"]
    session_total["hit_rate_pct"] = round(
        (session_total["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
    )

    def _top_rows(rows, columns):
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

    return {
        "range": range,
        "project": project,
        "per_model": per_model,
        "session_total": session_total,
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


@router.get("/context-growth/agg")
@cache_response
def context_growth_agg(
    range: str = Query("30d"),
    project: str | None = Query(None),
) -> dict:
    """Distribution stats for context size, computed two ways:
       - per_turn: every turn across every file in scope (input distribution)
       - per_session_final: the LAST turn of each MAIN file's ctx_turns
    Returns mean, p50, p90, p99, max, n for both."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)

    with db.viz_conn() as c:
        per_turn = c.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              AVG(input_int) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY input_int) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY input_int) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY input_int) AS p99,
              MAX(input_int) AS max
            FROM (
              SELECT ((turn->>'input')::int) AS input_int
              FROM files f, jsonb_array_elements(f.ctx_turns) AS turn
              WHERE f.r2_last_modified >= %s {proj_filter}
            ) t
            """,
            args,
        ).fetchone()

        per_session = c.execute(
            f"""
            SELECT
              COUNT(*) AS n,
              AVG(final_input) AS mean,
              PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY final_input) AS p50,
              PERCENTILE_CONT(0.9)  WITHIN GROUP (ORDER BY final_input) AS p90,
              PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY final_input) AS p99,
              MAX(final_input) AS max
            FROM (
              SELECT ((f.ctx_turns -> -1 ->> 'input')::int) AS final_input
              FROM files f
              WHERE f.is_main = TRUE
                AND f.r2_last_modified >= %s {proj_filter}
                AND jsonb_array_length(f.ctx_turns) > 0
            ) t
            """,
            args,
        ).fetchone()

    def _stats(row):
        if row is None:
            return {"n": 0, "mean": 0, "p50": 0, "p90": 0, "p99": 0, "max": 0}
        n, mean, p50, p90, p99, mx = row
        return {
            "n": int(n or 0),
            "mean": int(mean or 0),
            "p50": int(p50 or 0),
            "p90": int(p90 or 0),
            "p99": int(p99 or 0),
            "max": int(mx or 0),
        }

    return {
        "range": range,
        "project": project,
        "per_turn": _stats(per_turn),
        "per_session_final": _stats(per_session),
    }


@router.get("/context-growth/session/{session_id}")
def context_growth_session(session_id: str) -> dict:
    """Per-turn array for the MAIN file of this session, mirroring
    parse_session.py:compute_context_growth output exactly."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, ctx_turns, turn_count "
            "FROM files WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, turns, count = row
    final_ctx = 0
    if turns:
        try:
            final_ctx = int(turns[-1].get("input", 0))
        except (KeyError, IndexError, TypeError):
            final_ctx = 0
    return {
        "session_id": session_id,
        "file_key": file_key,
        "turns": turns,
        "total_turns": count,
        "final_ctx": final_ctx,
    }


@router.get("/sessions/{session_id}/transcript")
def get_transcript(session_id: str) -> Response:
    """Stream raw jsonl from R2 via 20-min idle LRU. The MAIN file of the
    session is what's returned (the agent peers are visible only via the
    Inspector's per-file dropdown, future work)."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key, r2_etag FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key, etag = row
    body = cache.transcript_cache.get(etag)
    if body is None:
        body = r2.get_object(file_key)
        cache.transcript_cache.put(etag, body)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"ETag": etag, "Cache-Control": "no-cache"},
    )


@router.get("/sessions/{session_id}/sidecar")
def get_sidecar(
    session_id: str,
    path: str = Query(..., min_length=1),
) -> Response:
    """Path-validated sidecar fetch from R2 under the session's prefix."""
    with db.viz_conn() as c:
        row = c.execute(
            "SELECT file_key FROM files "
            "WHERE session_id = %s AND is_main = TRUE LIMIT 1",
            (session_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "session not found")
    file_key = row[0]
    session_prefix = file_key.rsplit("/", 1)[0] + "/"
    if path.startswith("/") or ".." in path.split("/"):
        raise HTTPException(400, "bad path")
    full_key = session_prefix + path
    # Data files may be stored xz-compressed (`<name>.xz`); try the plain key
    # first, then the compressed one. r2.get_object inflates `.xz` transparently,
    # so the body is the original bytes and media type keys off the plain path.
    body = None
    for candidate in (full_key, full_key + ".xz"):
        try:
            body = r2.get_object(candidate)
            break
        except Exception:
            continue
    if body is None:
        raise HTTPException(404, "sidecar not found")
    media = "text/plain"
    if path.endswith(".jsonl"):
        media = "application/x-ndjson"
    elif path.endswith(".json"):
        media = "application/json"
    return Response(content=body, media_type=media)


# ---------------------------------------------------------------------------
# Legacy compatibility shims (R11). Restored frontend expects these.
# Source data lives in the new files+records tables; the response shape is
# the OLD pre-R9 shape so backendDashToShape / SessionsList work unchanged.
# ---------------------------------------------------------------------------


def _iso(v) -> str | None:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


@router.get("/dashboard")
@cache_response
def dashboard(
    range: str = Query("30d"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Hourly aggregates + per-session burns + per-session ctx_lines.

    Cross-file uuid dedup comes from records.is_canonical, resolved at
    ingest (SV-CANONICAL-FLAG). `model=opus-4-7` filters the deduped CTE
    so every CTE-derived panel (hourly, cost_by_model, response_sizes,
    sessions, ctx_traces) is constrained to records matching the model
    substring."""
    delta = _parse_range(range)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    model_filter = ""
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")
    # The JOIN exists only to resolve project_id; without a project filter
    # it is a join against every file for nothing.
    join_files = "JOIN files f ON f.file_key = r.file_key" if proj_filter else ""

    # One scan filtering a boolean, where this used to be a DISTINCT ON
    # sort over the whole table UNION ALL'd with the NULL-uuid leg. The
    # model filter now applies to NULL-uuid rows too — the old NULL leg
    # silently exempted them.
    base_cte = f"""
    WITH deduped AS (
      SELECT r.file_key, r.line_num, r.uuid, r.ts, r.model,
             r.fresh_tokens, r.cache_read_tokens,
             r.output_tokens, r.cost_usd,
             r.text_chars
      FROM records r
      {join_files}
      WHERE r.is_canonical AND r.ts >= %s {proj_filter} {model_filter}
    )
    """

    # Pre-aggregated source for everything that is a pure sum/count/min/max.
    # `usage_rollup` is grain (session_id, hour, model) and is rebuilt at
    # ingest, so the panels below read numbers that are already summed
    # instead of re-aggregating every canonical record on each request.
    #
    # It is only usable when the display bucket is at least an hour wide —
    # the 24h view buckets at 5 minutes, which an hourly rollup cannot
    # express. For those the live subquery below is shaped with the SAME
    # column names, so every query after this point is written once.
    use_rollup = bucket_s >= 3600
    roll_args: list[Any] = [since]
    roll_proj = _proj_rollup(project, roll_args)
    roll_model = "AND u.model LIKE %s" if model else ""
    if model:
        roll_args.append(f"%{model}%")
    if use_rollup:
        roll_from = "usage_rollup u"
        # date_trunc so the partial hour containing `since` is included
        # rather than silently dropped.
        roll_since = "u.hour >= date_trunc('hour', %s::timestamptz)"
    else:
        roll_from = """(
          SELECT f.session_id, f.project_id, f.is_main,
                 r.ts AS hour, r.ts AS first_ts, r.ts AS last_ts,
                 COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                 1::bigint AS requests,
                 r.fresh_tokens, r.output_tokens,
                 r.cache_read_tokens, r.cost_usd
            FROM records r
            JOIN files f ON f.file_key = r.file_key
           WHERE r.is_canonical AND r.ts IS NOT NULL
        ) u"""
        roll_since = "u.hour >= %s"
    roll_src = f"""
        FROM {roll_from}
        WHERE {roll_since} {roll_proj} {roll_model}
    """

    ph = Phases("dashboard")

    with db.viz_conn() as c:
        hourly_rows = ph.execute(
            "hourly", c,
            f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM u.hour) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS hour,
                   u.model,
                   SUM(u.fresh_tokens)      AS input_tokens,
                   SUM(u.output_tokens)     AS output_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cost_usd)          AS cost_usd,
                   SUM(u.requests)          AS requests,
                   COUNT(DISTINCT u.session_id) AS session_count
            {roll_src}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            roll_args,
        ).fetchall()

        # Percentiles are the one thing that cannot be rolled up — p50/p90
        # of a union of hours is not derivable from per-hour p50/p90 — so
        # this stays a live pass, narrowed to the text-bearing rows.
        response_sizes_rows = ph.execute(
            "response_sizes", c,
            base_cte + f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {bucket_s}) * {bucket_s} + {bucket_s} / 2
                   ) AS bucket,
                   COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY d.text_chars) AS p50,
                   PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.text_chars) AS p90
            FROM deduped d
            WHERE d.text_chars > 0 AND d.ts IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
            args,
        ).fetchall()

        total_sessions_row = ph.execute(
            "total_sessions", c,
            f"""
            SELECT COUNT(DISTINCT u.session_id) AS n
            {roll_src}
            """,
            roll_args,
        ).fetchone()
        total_sessions = int(total_sessions_row[0] or 0) if total_sessions_row else 0

        file_counts_args = list(args)
        file_counts_row = ph.execute(
            "file_counts", c,
            f"""
            -- The EXISTS predicates were correlated subqueries evaluated
            -- once per file row (four of them, across every file).
            -- Resolve each to a set once and LEFT JOIN instead.
            WITH files_with_records AS (
              SELECT file_key FROM records GROUP BY file_key
            ),
            sessions_with_main AS (
              SELECT session_id FROM files WHERE is_main GROUP BY session_id
            )
            SELECT
              COUNT(*) FILTER (
                WHERE f.is_main AND fr.file_key IS NOT NULL
              ) AS main_w_usage,
              COUNT(*) FILTER (
                WHERE f.is_main AND fr.file_key IS NULL
              ) AS main_empty,
              COUNT(*) FILTER (WHERE NOT f.is_main) AS subagent_files,
              COUNT(DISTINCT f.session_id) FILTER (
                WHERE sm.session_id IS NULL AND fr.file_key IS NOT NULL
              ) AS subagent_only_sessions
            FROM files f
            LEFT JOIN files_with_records fr ON fr.file_key = f.file_key
            LEFT JOIN sessions_with_main   sm ON sm.session_id = f.session_id
            WHERE f.r2_last_modified >= %s {proj_filter}
            """,
            file_counts_args,
        ).fetchone()
        main_w_usage           = int(file_counts_row[0] or 0) if file_counts_row else 0
        main_empty             = int(file_counts_row[1] or 0) if file_counts_row else 0
        subagent_files         = int(file_counts_row[2] or 0) if file_counts_row else 0
        subagent_only_sessions = int(file_counts_row[3] or 0) if file_counts_row else 0

        # The rollup already carries per-(session, hour, model) sums, so the
        # dominant model is argmax(requests) over the in-range rows — exactly
        # what MODE() WITHIN GROUP computed from the raw records, without
        # re-sorting them.
        sessions_rows = ph.execute(
            "sessions", c,
            f"""
            WITH per_session_model AS (
              SELECT u.session_id, u.model,
                     SUM(u.requests)          AS requests,
                     SUM(u.fresh_tokens)      AS input_tokens,
                     SUM(u.output_tokens)     AS output_tokens,
                     SUM(u.cache_read_tokens) AS cache_read_tokens,
                     SUM(u.cost_usd)          AS cost_usd,
                     MIN(u.first_ts)          AS first_ts,
                     MAX(u.last_ts)           AS last_ts
              {roll_src}
              GROUP BY 1, 2
            )
            SELECT session_id,
                   EXTRACT(EPOCH FROM MIN(first_ts))::float AS start_ts,
                   EXTRACT(EPOCH FROM MAX(last_ts))::float  AS end_ts,
                   SUM(requests)          AS requests,
                   SUM(input_tokens)      AS input_tokens,
                   SUM(output_tokens)     AS output_tokens,
                   SUM(cache_read_tokens) AS cache_read_tokens,
                   SUM(cost_usd)          AS cost_usd,
                   -- Prefer the most-used REAL model; a session whose rows
                   -- carry no resolved model still gets a label rather than
                   -- going blank.
                   (ARRAY_AGG(model ORDER BY
                      (model <> 'unknown') DESC,
                      requests DESC))[1] AS model,
                   -- Every distinct real model the session used, so
                   -- per-model panels include a session even when the
                   -- model isn't the dominant one.
                   ARRAY_REMOVE(
                     ARRAY_AGG(DISTINCT model) FILTER (
                       WHERE model <> 'unknown'
                     ),
                     NULL
                   ) AS models_used
            FROM per_session_model
            GROUP BY session_id
            ORDER BY SUM(cost_usd) DESC NULLS LAST
            LIMIT 500
            """,
            roll_args,
        ).fetchall()

        # Response-size time series per model — daily-bucketed
        # text_chars median and p90 of VISIBLE response content (text
        # blocks only). Per analyst (2026-05-07), output_tokens
        # silently includes thinking — and per-model thinking shares
        # vary 0.7%–25%, so token-based percentiles conflate "longer
        # responses" with "more thinking". Character count of text
        # content blocks is the clean, model-fair "visible response
        # size" measure.
        # Per-FILE ctx traces — one row per main file AND per sub-agent
        # file with usage. The "Per-Session Context Growth" panel
        # treats each file as its own conversation, so a sub-agent
        # invocation surfaces under whatever model it ran on, even if
        # there's no main session file on disk.
        ctx_traces_args = list(args)
        ctx_traces_rows = ph.execute(
            "ctx_traces", c,
            f"""
            WITH scoped_files AS (
              SELECT f.file_key, f.session_id, f.is_main, f.ctx_turns
              FROM files f
              WHERE f.r2_last_modified >= %s {proj_filter}
                AND jsonb_array_length(f.ctx_turns) > 0
            ),
            -- Scoped to the files actually returned. Unrestricted, this
            -- ran an ordered-set aggregate over every record in the
            -- table on every request, ignoring both range and project.
            file_models AS (
              SELECT r.file_key,
                     COALESCE(
                       MODE() WITHIN GROUP (ORDER BY r.model) FILTER (
                         WHERE r.model <> ''
                       ),
                       MODE() WITHIN GROUP (ORDER BY NULLIF(r.model, ''))
                     ) AS model
              FROM records r
              WHERE r.file_key IN (SELECT file_key FROM scoped_files)
              GROUP BY r.file_key
            )
            SELECT sf.file_key, sf.session_id, sf.is_main,
                   COALESCE(fm.model, '') AS model,
                   sf.ctx_turns
            FROM scoped_files sf
            LEFT JOIN file_models fm ON fm.file_key = sf.file_key
            """,
            ctx_traces_args,
        ).fetchall()

        # Was two passes over `records` — a per-file GROUP BY plus a
        # correlated per-file dominant-model subquery that ran for every
        # main file in the table, ignoring both range and project. The
        # rollup carries is_main, per-model request counts and first/last
        # ts, so both collapse into one grouping over pre-summed rows.
        burn_rows = ph.execute(
            "burn", c,
            f"""
            WITH per_session_model AS (
              SELECT u.session_id, u.model,
                     SUM(u.requests)     AS requests,
                     SUM(u.fresh_tokens) AS write_tokens,
                     MIN(u.first_ts)     AS first_ts,
                     MAX(u.last_ts)      AS last_ts
              {roll_src} AND u.is_main
              GROUP BY 1, 2
            )
            SELECT session_id,
                   (SUM(write_tokens) / GREATEST(
                      EXTRACT(EPOCH FROM (MAX(last_ts) - MIN(first_ts))), 1.0
                    ))::float AS tps,
                   COALESCE((ARRAY_AGG(model ORDER BY
                     (model <> 'unknown') DESC, requests DESC))[1], '') AS model
            FROM per_session_model
            GROUP BY session_id
            ORDER BY tps DESC NULLS LAST
            LIMIT 200
            """,
            roll_args,
        ).fetchall()

        ctx_args = list(args)
        ctx_rows = ph.execute(
            "ctx_lines", c,
            f"""
            -- The ordering cost was a correlated SUM over `records`
            -- evaluated once per candidate file. Aggregate per file_key
            -- once and join, so the sort reads a prepared column.
            WITH cost_per_file AS (
              SELECT file_key, SUM(cost_usd) AS cost
              FROM records GROUP BY file_key
            )
            SELECT f.session_id, f.ctx_turns
            FROM files f
            LEFT JOIN cost_per_file cf ON cf.file_key = f.file_key
            WHERE f.is_main
              AND f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.ctx_turns) > 0
            ORDER BY COALESCE(cf.cost, 0) DESC
            LIMIT 20
            """,
            ctx_args,
        ).fetchall()

        rl_args = list(args)
        rl_rows = ph.execute(
            "rate_limits", c,
            f"""
            SELECT f.session_id, hit
            FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
            WHERE f.r2_last_modified >= %s {proj_filter}
              AND jsonb_array_length(f.rate_limit_hits) > 0
            """,
            rl_args,
        ).fetchall()

    hourly = []
    seen_hours: set[str | None] = set()
    # cost_by_model and response_sizes are folded out of these same rows
    # rather than costing their own full pass over the records.
    cost_by_model_acc: dict[str, float] = {}
    for row in hourly_rows:
        (hour, model, input_t, output_t, cr, cost, reqs, sc) = row
        hour_iso = _iso(hour)
        is_first_for_hour = hour_iso not in seen_hours
        seen_hours.add(hour_iso)
        model_name = model or "unknown"
        hourly.append({
            "hour": hour_iso,
            "model": model_name,
            "input_tokens": int(input_t or 0),
            "output_tokens": int(output_t or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "requests": int(reqs or 0),
            "session_count": int(sc or 0) if is_first_for_hour else 0,
        })
        cost_by_model_acc[model_name] = (
            cost_by_model_acc.get(model_name, 0.0) + float(cost or 0)
        )

    response_sizes = [
        {
            "ts": _iso(bucket),
            "model": m,
            "n": int(n or 0),
            "p50": float(p50 or 0),
            "p90": float(p90 or 0),
        }
        for (bucket, m, n, p50, p90) in response_sizes_rows
    ]

    burns = []
    for sid, tps, model in burn_rows:
        burns.append({
            "session_id": sid,
            "tps": float(tps or 0),
            "model": model or "",
            "hit_5h_limit": False,
        })

    def _parse_iso_to_epoch(s):
        try:
            if not s:
                return None
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    ctx_lines = []
    for sid, turns in ctx_rows:
        trace = []
        for t in (turns or []):
            try:
                ts_epoch = _parse_iso_to_epoch(t.get("ts"))
                ctx_val = int(t.get("input", 0))
            except (AttributeError, TypeError, ValueError):
                continue
            if ts_epoch is None:
                continue
            trace.append({"t": ts_epoch, "ctx": ctx_val})
        if trace:
            ctx_lines.append({"session_id": sid, "trace": trace})

    cost_by_model = sorted(
        ({"model": m, "cost_usd": v} for m, v in cost_by_model_acc.items() if v > 0),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )

    # ctx_turns used to be its own query, but it is a strict subset of
    # ctx_traces (main files only) — the same jsonb scan run twice.
    ctx_turns_by_session = {
        sid: turns
        for (fk, sid, is_main, mdl, turns) in ctx_traces_rows
        if is_main
    }
    sessions_out = []
    for row in sessions_rows:
        (sid, st, et, reqs, inp, out, cr, cost, dom, models_used) = row
        raw_turns = ctx_turns_by_session.get(sid) or []
        # Project to {t, ctx} (input is total ctx-window: input + cache_read).
        turns_proj = [
            {"t": i, "ctx": int(t.get("input", 0) or 0)}
            for i, t in enumerate(raw_turns)
            if isinstance(t, dict)
        ]
        # null (not 0) when ctx_turns is empty so the UI can flag the dot
        # as "ctx unknown" instead of silently falling back to a synthetic
        # duration-based size encoding (analyst spec 2026-05-07).
        ctx_at_end = turns_proj[-1]["ctx"] if turns_proj else None
        sessions_out.append({
            "session_id": sid,
            "start_ts": float(st or 0),
            "end_ts": float(et or 0),
            "requests": int(reqs or 0),
            "input_tokens": int(inp or 0),
            "output_tokens": int(out or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "model": dom or "",
            "models_used": list(models_used or []),
            "ctx_at_end": ctx_at_end,
            "turns": turns_proj,
        })

    rate_limit_hits = []
    for sid, hit in rl_rows:
        ts_str = (hit or {}).get("ts") or ""
        if not ts_str:
            continue
        rate_limit_hits.append({
            "session_id": sid,
            "ts": ts_str,
            "content": (hit or {}).get("content", ""),
        })

    ph.done(range=range, project=project, model=model)

    return {
        "range": range,
        "project": project,
        "bucket_s": bucket_s,
        "hourly": hourly,
        "cost_by_model": cost_by_model,
        "rate_limit_hits": rate_limit_hits,
        "burns": burns,
        "sessions": sessions_out,
        "total_sessions": total_sessions,
        "main_w_usage": main_w_usage,
        "main_empty": main_empty,
        "subagent_files": subagent_files,
        "subagent_only_sessions": subagent_only_sessions,
        "ctx_traces": [
            {
                "file_key": fk,
                "session_id": sid,
                "is_main": bool(is_main),
                "model": model or "",
                "turns": [
                    {"t": i, "ctx": int(t.get("input", 0) or 0)}
                    for i, t in enumerate(turns or [])
                    if isinstance(t, dict)
                ],
            }
            for (fk, sid, is_main, model, turns) in ctx_traces_rows
        ],
        "response_sizes": response_sizes,
        "ctx_lines": ctx_lines,
    }


def _aggregate_session_row(row) -> dict:
    """Shared row-builder for /api/sessions and /api/sessions/{id}."""
    (
        session_id, project_id, first_at, last_at, dur_s, req_count,
        input_t, output_t, cr, cost, models_raw,
    ) = row
    models = {}
    if models_raw:
        # models_raw comes as a list of (model, count) pairs from a json_agg.
        for entry in models_raw:
            try:
                models[entry["model"]] = int(entry["count"])
            except (KeyError, TypeError, ValueError):
                continue
    return {
        "session_id": session_id,
        "project_id": project_id,
        "first_event_at": _iso(first_at),
        "last_event_at": _iso(last_at),
        "duration_s": int(dur_s or 0),
        "request_count": int(req_count or 0),
        "input_tokens": int(input_t or 0),
        "output_tokens": int(output_t or 0),
        "cache_read_tokens": int(cr or 0),
        "cost_usd": float(cost or 0),
        "models": models,
        "limit_hits": 0,
    }


@router.get("/sessions")
def list_sessions(
    project: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    cursor: str | None = Query(None),
) -> dict:
    """Paginated MAIN-file session list. Cursor = ISO ts of first_event_at
    (descending); pass the next_cursor from the prior page to continue.

    Aggregates fresh from the records table (no separate rollup). The
    `models` field is built from a sub-aggregation; `limit_hits` returns
    0 because the new schema doesn't track rate-limit hits per-session
    (the OLD column came from a removed join).
    """
    args: list[Any] = []
    proj_filter = _proj_filter(project, args)
    join_files = "JOIN files f ON f.file_key = r.file_key" if proj_filter else ""

    cursor_clause = ""
    if cursor:
        try:
            cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"bad cursor: {cursor!r}")
        cursor_clause = "WHERE first_event_at < %s"
        cursor_arg: list[Any] = [cursor_dt]
    else:
        cursor_arg = []

    # Aggregate across ALL files of each session (main + agent-* sub-files)
    # with cross-file uuid dedup, mirroring /api/dashboard. Sub-agent
    # tokens/cost roll up into the parent session's totals; the session
    # is keyed by session_id (shared between main + its agent files).
    # The dedup leg used to be interpolated twice (one per UNION arm) from
    # a `proj_filter` whose bind param was appended once — so a project
    # filter here produced two placeholders for one argument. Collapsing to
    # a single is_canonical scan removes the second interpolation and the
    # mismatch with it.
    sql = f"""
    WITH deduped AS (
      SELECT r.file_key, r.uuid, r.ts, r.model,
             r.fresh_tokens, r.cache_read_tokens,
             r.output_tokens, r.cost_usd
      FROM records r {join_files}
      WHERE r.is_canonical {proj_filter}
    ),
    per_session AS (
      SELECT f.session_id,
             min(f.project_id) AS project_id,
             min(d.ts) AS first_event_at,
             max(d.ts) AS last_event_at,
             EXTRACT(EPOCH FROM (max(d.ts) - min(d.ts)))::bigint AS duration_s,
             COUNT(*) AS request_count,
             SUM(d.fresh_tokens)         AS input_tokens,
             SUM(d.output_tokens)        AS output_tokens,
             SUM(d.cache_read_tokens)    AS cache_read_tokens,
             SUM(d.cost_usd)             AS cost_usd,
             (SELECT json_agg(json_build_object('model', model, 'count', c))
              FROM (
                SELECT d2.model, COUNT(*) AS c
                FROM deduped d2
                JOIN files f2 ON f2.file_key = d2.file_key
                WHERE f2.session_id = f.session_id AND d2.model <> ''
                GROUP BY d2.model
              ) sub) AS models_raw
      FROM deduped d
      JOIN files f ON f.file_key = d.file_key
      GROUP BY f.session_id
    )
    SELECT * FROM per_session
    {cursor_clause}
    ORDER BY first_event_at DESC NULLS LAST
    LIMIT %s
    """

    with db.viz_conn() as c:
        rows = c.execute(sql, args + cursor_arg + [limit + 1]).fetchall()

    items = [_aggregate_session_row(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        # The cursor is the first_event_at of the NEXT page's first row,
        # which is the last item in `items` (we paged DESC).
        last_first = items[-1]["first_event_at"]
        next_cursor = last_first
    return {"items": items, "next_cursor": next_cursor}


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    """Single-session aggregation including ctx_trace and burn rate.

    `ctx_trace` is the canonical files.ctx_turns array reshaped to
    [{t: epoch_seconds, ctx: int}] for the OLD frontend chart code.
    `burn` is {tps, model} computed from the records table.
    `r2_key` is the MAIN file_key.
    `limit_hits` returns 0 (see /api/sessions docstring).
    """
    with db.viz_conn() as c:
        row = c.execute(
            """
            WITH per_session AS (
              SELECT f.session_id,
                     f.project_id,
                     f.file_key,
                     f.ctx_turns,
                     min(r.ts) AS first_event_at,
                     max(r.ts) AS last_event_at,
                     EXTRACT(EPOCH FROM (max(r.ts) - min(r.ts)))::bigint AS duration_s,
                     COUNT(*) AS request_count,
                     SUM(r.fresh_tokens)         AS input_tokens,
                     SUM(r.output_tokens)        AS output_tokens,
                     SUM(r.cache_read_tokens)    AS cache_read_tokens,
                     SUM(r.cost_usd)             AS cost_usd,
                     (SELECT json_agg(json_build_object('model', model, 'count', c))
                      FROM (
                        SELECT model, COUNT(*) AS c
                        FROM records r2
                        WHERE r2.file_key = f.file_key AND r2.model <> ''
                        GROUP BY model
                      ) sub) AS models_raw,
                     (SELECT model FROM records r3
                      WHERE r3.file_key = f.file_key AND r3.model <> ''
                      GROUP BY model ORDER BY count(*) DESC LIMIT 1
                     ) AS dom_model
              FROM files f
              LEFT JOIN records r ON r.file_key = f.file_key
              WHERE f.session_id = %s AND f.is_main = TRUE
              GROUP BY f.session_id, f.project_id, f.file_key, f.ctx_turns
              LIMIT 1
            )
            SELECT * FROM per_session
            """,
            (session_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(404, "session not found")

    (
        sid, project_id, file_key, ctx_turns,
        first_at, last_at, dur_s, req_count,
        input_t, output_t, cr, cost,
        models_raw, dom_model,
    ) = row

    base = _aggregate_session_row((
        sid, project_id, first_at, last_at, dur_s, req_count,
        input_t, output_t, cr, cost, models_raw,
    ))

    # ctx_trace from ctx_turns (already canonical [{idx,ts,line,input,output,delta}])
    ctx_trace = []
    for t in (ctx_turns or []):
        ts_str = t.get("ts") if isinstance(t, dict) else None
        try:
            if ts_str:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                t_epoch = int(dt.timestamp())
            else:
                t_epoch = None
            ctx_val = int(t.get("input", 0))
        except (AttributeError, TypeError, ValueError):
            continue
        if t_epoch is None:
            continue
        ctx_trace.append({"t": t_epoch, "ctx": ctx_val})

    # Burn (tps + dominant model) for this session.
    write_tokens = base["input_tokens"]
    span_s = max(base["duration_s"], 1)
    burn = {
        "tps": float(write_tokens) / span_s,
        "model": dom_model or "",
        "hit_5h_limit": False,
    }

    return {
        **base,
        "r2_key": file_key,
        "ctx_trace": ctx_trace,
        "burn": burn,
    }
