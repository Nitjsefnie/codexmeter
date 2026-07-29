"""/api/dashboard: hourly aggregates + per-session totals + ctx traces.

Split out of backend/api.py to keep every module under pylint's
module-length limit. The shared query helpers (_proj_*, _parse_range,
_bucket_seconds, Phases, _iso) stay in backend.api and are imported here.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, Query
from starlette.requests import Request

from backend import db
from backend.api import (
    Phases,
    _bucket_seconds,
    _iso,
    _parse_range,
    _proj_filter,
    _proj_rollup,
    _proj_tool,
    _tool_source,
)
from backend.cache import cache_response

router = APIRouter(prefix="/api")


class _DashQuery(NamedTuple):
    """Everything the dashboard queries need, resolved once up front."""
    range_: str
    project: str | None
    model: str | None
    bucket_s: int
    args: list[Any]        # records-scoped args: since + project + model
    proj_filter: str
    file_args: list[Any]   # snapshot before the model param was appended
    base_cte: str
    roll_src: str
    roll_args: list[Any]


class _DashRows(NamedTuple):
    """Raw query results, handed to the payload builders."""
    hourly: list
    response_sizes: list
    total_sessions: int
    cost_by_project: list
    file_counts: tuple[int, int, int, int]
    sessions: list
    ctx_traces: list
    rate_limits: list
    churn: list


def _base_cte(proj_filter: str, model_filter: str) -> str:
    # The JOIN exists only to resolve project_id; without a project filter
    # it is a join against every file for nothing.
    join_files = "JOIN files f ON f.file_key = r.file_key" if proj_filter else ""
    # One scan filtering a boolean, where this used to be a DISTINCT ON
    # sort over the whole table UNION ALL'd with the NULL-uuid leg. The
    # model filter now applies to NULL-uuid rows too — the old NULL leg
    # silently exempted them.
    return f"""
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


def _roll_source(bucket_s: int, since: datetime, project: str | None,
                 model: str | None) -> tuple[str, list[Any]]:
    """Pre-aggregated source for everything that is a pure sum/count/min/max.

    `usage_rollup` is grain (session_id, hour, model) and is rebuilt at
    ingest, so the panels read numbers that are already summed instead of
    re-aggregating every canonical record on each request.

    It is only usable when the display bucket is at least an hour wide —
    the 24h view buckets at 5 minutes, which an hourly rollup cannot
    express. For those the live subquery below is shaped with the SAME
    column names, so every query after this point is written once.
    """
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
    return roll_src, roll_args


def _dash_query(range_: str, project: str | None,
                model: str | None) -> _DashQuery:
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    bucket_s = _bucket_seconds(delta)
    model_filter = ""
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)
    # Snapshot BEFORE the model param is appended. The queries that read
    # `files` (file_counts, ctx_traces, ctx_lines, rate_limits) have no
    # model placeholder — handing them the full `args` passed one argument
    # more than the statement had placeholders, so any ?model= request
    # raised instead of returning a filtered dashboard.
    file_args: list[Any] = list(args)
    if model:
        model_filter = "AND r.model LIKE %s"
        args.append(f"%{model}%")
    return _DashQuery(
        range_, project, model, bucket_s, args, proj_filter, file_args,
        _base_cte(proj_filter, model_filter),
        *_roll_source(bucket_s, since, project, model),
    )


def _dash_rows(q: _DashQuery, ph: Phases) -> _DashRows:
    """Run all eight panel queries and return their raw rows."""
    with db.viz_conn() as c:
        # The rollups removed this endpoint's biggest sorts, but ctx_traces
        # and response_sizes still sort/aggregate over raw rows, and the
        # default work_mem spills them to disk on a wide range.
        c.execute("SET LOCAL work_mem = '64MB'")
        hourly_rows = ph.execute(
            "hourly", c,
            db.sql_literal(f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM u.hour) / {q.bucket_s}) * {q.bucket_s} + {q.bucket_s} / 2
                   ) AS hour,
                   u.model,
                   SUM(u.fresh_tokens)      AS input_tokens,
                   SUM(u.output_tokens)     AS output_tokens,
                   SUM(u.cache_read_tokens) AS cache_read_tokens,
                   SUM(u.cost_usd)          AS cost_usd,
                   SUM(u.requests)          AS requests,
                   COUNT(DISTINCT u.session_id) AS session_count
            {q.roll_src}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            q.roll_args,
        ).fetchall()

        # Percentiles are the one thing that cannot be rolled up — p50/p90
        # of a union of hours is not derivable from per-hour p50/p90 — so
        # this stays a live pass, narrowed to the text-bearing rows.
        response_sizes_rows = ph.execute(
            "response_sizes", c,
            db.sql_literal(q.base_cte + f"""
            SELECT to_timestamp(
                     floor(EXTRACT(EPOCH FROM d.ts) / {q.bucket_s}) * {q.bucket_s} + {q.bucket_s} / 2
                   ) AS bucket,
                   COALESCE(NULLIF(d.model, ''), 'unknown') AS model,
                   COUNT(*) AS n,
                   PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY d.text_chars) AS p50,
                   PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY d.text_chars) AS p90
            FROM deduped d
            WHERE d.text_chars > 0 AND d.ts IS NOT NULL
            GROUP BY 1, 2
            ORDER BY 1, 2
            """),
            q.args,
        ).fetchall()

        total_sessions_row = ph.execute(
            "total_sessions", c,
            db.sql_literal(f"""
            SELECT COUNT(DISTINCT u.session_id) AS n
            {q.roll_src}
            """),
            q.roll_args,
        ).fetchone()
        total_sessions = int(total_sessions_row[0] or 0) if total_sessions_row else 0

        # Per-project range cost for the "Cost by Project" panel, folded
        # out of the same rollup source as cost_by_model (range-, project-
        # and model-filtered alike). Sorted DESC here so the top-10/Other
        # fold below is a plain slice.
        cost_by_project_rows = ph.execute(
            "cost_by_project", c,
            db.sql_literal(f"""
            SELECT u.project_id, SUM(u.cost_usd) AS cost_usd
            {q.roll_src}
            GROUP BY 1
            ORDER BY 2 DESC
            """),
            q.roll_args,
        ).fetchall()

        file_counts_row = ph.execute(
            "file_counts", c,
            db.sql_literal(f"""
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
            WHERE f.r2_last_modified >= %s {q.proj_filter}
            """),
            list(q.file_args),
        ).fetchone()
        file_counts = (
            (0, 0, 0, 0)
            if file_counts_row is None
            else (
                int(file_counts_row[0] or 0),
                int(file_counts_row[1] or 0),
                int(file_counts_row[2] or 0),
                int(file_counts_row[3] or 0),
            )
        )

        # The rollup already carries per-(session, hour, model) sums, so the
        # dominant model is argmax(requests) over the in-range rows — exactly
        # what MODE() WITHIN GROUP computed from the raw records, without
        # re-sorting them.
        sessions_rows = ph.execute(
            "sessions", c,
            db.sql_literal(f"""
            WITH per_session_model AS (
              SELECT u.session_id, u.model,
                     SUM(u.requests)          AS requests,
                     SUM(u.fresh_tokens)      AS input_tokens,
                     SUM(u.output_tokens)     AS output_tokens,
                     SUM(u.cache_read_tokens) AS cache_read_tokens,
                     SUM(u.cost_usd)          AS cost_usd,
                     MIN(u.first_ts)          AS first_ts,
                     MAX(u.last_ts)           AS last_ts
              {q.roll_src}
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
            """),
            q.roll_args,
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
        ctx_traces_rows = ph.execute(
            "ctx_traces", c,
            db.sql_literal(f"""
            WITH scoped_files AS (
              SELECT f.file_key, f.session_id, f.is_main, f.ctx_turns
              FROM files f
              WHERE f.r2_last_modified >= %s {q.proj_filter}
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
            """),
            list(q.file_args),
        ).fetchall()

        rl_args = list(q.file_args) + [q.args[0]]
        rl_rows = ph.execute(
            "rate_limits", c,
            db.sql_literal(f"""
            SELECT f.session_id, hit
            FROM files f, jsonb_array_elements(f.rate_limit_hits) AS hit
            WHERE f.r2_last_modified >= %s {q.proj_filter}
              AND jsonb_array_length(f.rate_limit_hits) > 0
              -- r2_last_modified is the file's mtime, not the hit's time:
              -- a file touched within range can still carry hits older
              -- than `since`, so filter on each hit's own ts too. The
              -- mtime clause stays as the coarse, indexable pre-filter.
              --
              -- The cast is guarded, because casting a malformed ts RAISES
              -- (`invalid input syntax for type timestamp with time zone`)
              -- and the traceback would take out the WHOLE dashboard, not
              -- just this panel. Two halves to the guard:
              --   * pg_input_is_valid, not a regex — a shape test admits
              --     '2026-13-45T99:99:99Z' and '2026-02-30T00:00:00Z',
              --     which look like timestamps and still raise on cast.
              --     (PG16+; production is 17 and CI runs postgres:16.)
              --   * CASE, not `AND valid AND cast` — the planner may
              --     evaluate a bare cast before the test protecting it,
              --     while CASE's evaluation order is guaranteed.
              -- A hit that fails the test yields NULL, which fails the >=
              -- and is dropped, which is what we want for junk.
              AND (CASE WHEN pg_input_is_valid(hit->>'ts', 'timestamptz')
                        THEN (hit->>'ts')::timestamptz END) >= %s
            """),
            rl_args,
        ).fetchall()

        churn_rows = _dash_churn_rows(c, q, ph)

    return _DashRows(
        hourly_rows, response_sizes_rows, total_sessions,
        cost_by_project_rows, file_counts, sessions_rows, ctx_traces_rows,
        rl_rows, churn_rows,
    )


def _dash_churn_rows(c, q: _DashQuery, ph: Phases) -> list:
    """Lines added/deleted per bucket — two separate POSITIVE series
    (issue #17), read off the same dual-path tool source as the tool
    endpoints: tool_rollup at >= 1h buckets, the live tool_uses
    subquery below that. No model dimension exists on tool_uses
    (its line_nums are disjoint from records'), so ?model= does not
    constrain this series — same caveat the tool endpoints carry."""
    args: list[Any] = [q.args[0]]  # since
    proj = _proj_tool(q.project, args)
    return ph.execute(
        "churn", c,
        db.sql_literal(f"""
        SELECT to_timestamp(
                 floor(EXTRACT(EPOCH FROM t.hour) / {q.bucket_s}) * {q.bucket_s} + {q.bucket_s} / 2
               ) AS bucket,
               SUM(t.lines_added)   AS lines_added,
               SUM(t.lines_deleted) AS lines_deleted
        FROM {_tool_source(q.bucket_s)}
        WHERE t.hour >= %s {proj}
        GROUP BY 1
        ORDER BY 1
        """),
        args,
    ).fetchall()


def _dash_cost_by_model(hourly: list) -> list:
    """cost_by_model folded out of the hourly rows rather than costing
    its own full pass over the records."""
    acc: dict[str, float] = {}
    for h in hourly:
        acc[h["model"]] = acc.get(h["model"], 0.0) + h["cost_usd"]
    return sorted(
        ({"model": m, "cost_usd": v} for m, v in acc.items() if v > 0),
        key=lambda r: r["cost_usd"],
        reverse=True,
    )


def _dash_hourly(hourly_rows: list) -> list:
    hourly = []
    seen_hours: set[str | None] = set()
    for row in hourly_rows:
        (hour, row_model, input_t, output_t, cr, cost, reqs, sc) = row
        hour_iso = _iso(hour)
        is_first_for_hour = hour_iso not in seen_hours
        seen_hours.add(hour_iso)
        hourly.append({
            "hour": hour_iso,
            # NOT `model` — that is the request's filter, and rebinding it
            # here would make the TIMING line report the last row's model
            # as the one that was asked for.
            "model": row_model or "unknown",
            "input_tokens": int(input_t or 0),
            "output_tokens": int(output_t or 0),
            "cache_read_tokens": int(cr or 0),
            "cost_usd": float(cost or 0),
            "requests": int(reqs or 0),
            "session_count": int(sc or 0) if is_first_for_hour else 0,
        })
    return hourly


def _dash_cost_by_project(cost_by_project_rows: list) -> list:
    """Top 10 by range cost; the tail collapses into ONE "Other" row.

    Zero-cost rows are noise (the project picker drops all-time-zero
    projects too), so they're excluded — a bar per project is unreadable
    at hundreds of projects.
    """
    cost_by_project_pos = [
        {"project": p or "unknown", "cost_usd": float(cost or 0)}
        for (p, cost) in cost_by_project_rows
        if float(cost or 0) > 0
    ]
    cost_by_project = cost_by_project_pos[:10]
    if len(cost_by_project_pos) > 10:
        rest = cost_by_project_pos[10:]
        cost_by_project.append({
            "project": f"Other ({len(rest)} projects)",
            "cost_usd": sum(r["cost_usd"] for r in rest),
        })
    return cost_by_project


def _dash_session_out(row, ctx_turns_by_session: dict) -> dict:
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
    return {
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
    }


def _dash_sessions(sessions_rows: list, ctx_turns_by_session: dict) -> list:
    return [
        _dash_session_out(row, ctx_turns_by_session) for row in sessions_rows
    ]


def _dash_rate_limits(rl_rows: list) -> list:
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
    return rate_limit_hits


def _dash_churn(churn_rows: list) -> list:
    """Bucketed edit churn — lines added / lines deleted as two separate
    positive series, the same per-bucket shape the token panels plot."""
    return [
        {
            "ts": _iso(bucket),
            "lines_added": int(added or 0),
            "lines_deleted": int(deleted or 0),
        }
        for (bucket, added, deleted) in churn_rows
    ]


def _dash_payload(q: _DashQuery, rows: _DashRows) -> dict:
    hourly = _dash_hourly(rows.hourly)
    # ctx_turns used to be its own query, but it is a strict subset of
    # ctx_traces (main files only) — the same jsonb scan run twice.
    ctx_turns_by_session = {
        sid: turns
        for (fk, sid, is_main, mdl, turns) in rows.ctx_traces
        if is_main
    }
    main_w_usage, main_empty, subagent_files, subagent_only_sessions = (
        rows.file_counts
    )
    return {
        "range": q.range_,
        "project": q.project,
        "bucket_s": q.bucket_s,
        "hourly": hourly,
        "cost_by_model": _dash_cost_by_model(hourly),
        "cost_by_project": _dash_cost_by_project(rows.cost_by_project),
        "rate_limit_hits": _dash_rate_limits(rows.rate_limits),
        "churn": _dash_churn(rows.churn),
        "sessions": _dash_sessions(rows.sessions, ctx_turns_by_session),
        "total_sessions": rows.total_sessions,
        "main_w_usage": main_w_usage,
        "main_empty": main_empty,
        "subagent_files": subagent_files,
        "subagent_only_sessions": subagent_only_sessions,
        # `turns` is a FLAT array of ctx values, positionally indexed.
        # It used to be [{"t": i, "ctx": n}, ...] where `t` was just the
        # array index the consumer re-derived anyway — roughly 20 bytes per
        # turn instead of 6, repeated across every turn in range.
        # session_id/is_main are dropped too: they are only used
        # server-side above (to fold in ctx_turns) and no consumer reads
        # them off the wire.
        "ctx_traces": [
            {
                "model": model or "",
                "turns": [
                    int(t.get("input", 0) or 0)
                    for t in (turns or [])
                    if isinstance(t, dict)
                ],
            }
            for (fk, sid, is_main, model, turns) in rows.ctx_traces
        ],
        "response_sizes": [
            {
                "ts": _iso(bucket),
                "model": m,
                "n": int(n or 0),
                "p50": float(p50 or 0),
                "p90": float(p90 or 0),
            }
            for (bucket, m, n, p50, p90) in rows.response_sizes
        ],
    }


@router.get("/dashboard")
def dashboard_route(
    request: Request,
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Route wrapper around the cached dashboard payload.

    The cached body always carries cost_by_project (one cache entry shared
    by every caller, so `cache.warm(api_dashboard.dashboard)` keeps working
    and a guest never triggers a second compute). Guests get the same payload
    MINUS that key: per-project names/costs are exactly what the guest
    gates on /api/projects and on project= exist to withhold (see
    session.auth_middleware), and /api/dashboard is guest-accessible. The
    dict comprehension copies rather than mutating so the cached object
    stays intact for later non-guest hits."""
    payload = dashboard(
        range_=range_, project=project, model=model, fresh=fresh
    )
    if bool(getattr(request.state, "is_guest", False)):
        payload = {k: v for k, v in payload.items() if k != "cost_by_project"}
    return payload


@cache_response
def dashboard(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
    model: str | None = Query(None),
    fresh: int = Query(0),
) -> dict:
    """Hourly aggregates + per-session totals + per-file ctx traces.

    Cross-file uuid dedup comes from records.is_canonical, resolved at
    ingest (SV-CANONICAL-FLAG). `model=opus-4-7` filters the deduped CTE
    so every CTE-derived panel (hourly, cost_by_model, response_sizes,
    sessions, ctx_traces) is constrained to records matching the model
    substring. cost_by_project is folded from the same rollup source as
    cost_by_model; the route wrapper strips it for guests."""
    q = _dash_query(range_, project, model)
    ph = Phases("dashboard")

    t_sql = time.perf_counter()
    rows = _dash_rows(q, ph)
    # The per-query labels only account for time inside execute(); these
    # two bracket everything, so a gap between sql_total and the sum of
    # the labels is row-fetch time and a large `build` is Python-side.
    ph.mark("sql_total", time.perf_counter() - t_sql)

    t_build = time.perf_counter()
    payload = _dash_payload(q, rows)
    ph.mark("build", time.perf_counter() - t_build)
    ph.done(range=range_, project=project, model=model)
    return payload
