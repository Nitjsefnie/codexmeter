"""/api/sessions* and /api/context-growth* endpoints.

Split out of backend/api.py to keep every module under pylint's
module-length limit. The shared query helpers (_proj_*, _parse_range,
_iso) stay in backend.api and are imported here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from starlette.responses import Response

from backend import cache, db, r2
from backend.api import _iso, _parse_range, _proj_filter
from backend.cache import cache_response

router = APIRouter(prefix="/api")


@router.get("/context-growth/agg")
@cache_response
def context_growth_agg(
    range_: str = Query("30d", alias="range"),
    project: str | None = Query(None),
) -> dict:
    """Distribution stats for context size, computed two ways:
       - per_turn: every turn across every file in scope (input distribution)
       - per_session_final: the LAST turn of each MAIN file's ctx_turns
    Returns mean, p50, p90, p99, max, n for both."""
    delta = _parse_range(range_)
    since = datetime.now(timezone.utc) - delta
    args: list[Any] = [since]
    proj_filter = _proj_filter(project, args)

    with db.viz_conn() as c:
        per_turn = c.execute(
            db.sql_literal(f"""
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
            """),
            args,
        ).fetchone()

        per_session = c.execute(
            db.sql_literal(f"""
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
            """),
            args,
        ).fetchone()

    return {
        "range": range_,
        "project": project,
        "per_turn": _stats(per_turn),
        "per_session_final": _stats(per_session),
    }


def _stats(row) -> dict:
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


def _cursor_args(cursor: str | None) -> tuple[str, list[Any]]:
    """(WHERE clause, args) for keyset pagination, or ("", []) on page 1."""
    if not cursor:
        return "", []
    try:
        cursor_dt = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(400, f"bad cursor: {cursor!r}") from exc
    return "WHERE first_event_at < %s", [cursor_dt]


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
    cursor_clause, cursor_arg = _cursor_args(cursor)

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
        rows = c.execute(
            db.sql_literal(sql), args + cursor_arg + [limit + 1]
        ).fetchall()

    items = [_aggregate_session_row(r) for r in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        # The cursor is the first_event_at of the NEXT page's first row,
        # which is the last item in `items` (we paged DESC).
        next_cursor = items[-1]["first_event_at"]
    return {"items": items, "next_cursor": next_cursor}


def _session_detail_row(session_id: str):
    """The aggregate row for one session's MAIN file, or None."""
    with db.viz_conn() as c:
        return c.execute(
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


def _ctx_trace(ctx_turns) -> list:
    """files.ctx_turns (canonical [{idx,ts,line,input,output,delta}])
    reshaped to [{t: epoch_seconds, ctx: int}] for the OLD frontend chart
    code."""
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
    return ctx_trace


@router.get("/sessions/{session_id}")
def session_detail(session_id: str) -> dict:
    """Single-session aggregation including ctx_trace and burn rate.

    `ctx_trace` is the canonical files.ctx_turns array reshaped to
    [{t: epoch_seconds, ctx: int}] for the OLD frontend chart code.
    `burn` is {tps, model} computed from the records table.
    `r2_key` is the MAIN file_key.
    `limit_hits` returns 0 (see /api/sessions docstring).
    """
    row = _session_detail_row(session_id)
    if row is None:
        raise HTTPException(404, "session not found")

    # row is (sid, project_id, file_key, ctx_turns, first_at, last_at,
    # dur_s, req_count, input_t, output_t, cr, cost, models_raw,
    # dom_model); _aggregate_session_row takes the row minus file_key,
    # ctx_turns and dom_model.
    base = _aggregate_session_row(row[:2] + row[4:13])

    # Burn (tps + dominant model) for this session.
    burn = {
        "tps": float(base["input_tokens"]) / max(base["duration_s"], 1),
        "model": row[13] or "",
        "hit_5h_limit": False,
    }

    return {
        **base,
        "r2_key": row[2],
        "ctx_trace": _ctx_trace(row[3]),
        "burn": burn,
    }
