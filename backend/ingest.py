"""R2 → Postgres ingest.

Per-file granularity: every wire.jsonl under bucket root → one row in `files`
+ N rows in `records` (per StatusUpdate). Cross-file uuid dedup is a
query-time concern.

Reparse trigger per FILE: row missing OR etag changed OR parser_version
mismatch. Orphan files (R2 key gone) are deleted. CASCADE drops records.

Kimi R2 layout:
  sessions/{session_hash}/{uuid}/wire.jsonl
  sessions/{session_hash}/{uuid}/context.jsonl
  sessions/{session_hash}/{uuid}/state.json

We ingest only wire.jsonl; project_id = session_hash, session_id = uuid.
"""
from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from backend import cache, db, events, parse, r2

log = logging.getLogger("kimimeter.ingest")


def run_ingest(trigger: str) -> dict:
    started = datetime.now(timezone.utc)
    parser_version = os.environ.get("PARSER_VERSION", "1")

    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO ingest_runs (started_at, trigger) VALUES (%s, %s) "
            "RETURNING id",
            (started, trigger),
        )
        run_id = cur.fetchone()[0]
        c.commit()

    listed = 0
    inserted = 0
    reparsed = 0
    deleted = 0
    err = None

    try:
        # Cache existing file_key → (etag, parser_version) for reparse decisions
        with db.viz_conn() as c:
            existing = {
                row[0]: (row[1], row[2])
                for row in c.execute(
                    "SELECT file_key, r2_etag, parser_version FROM files"
                ).fetchall()
            }

        # Pre-walk R2 once to (a) collect wire.jsonl keys and (b) discover
        # sessions/<HASH>/project.json markers. The marker is published by
        # archive_sessions.py on the originating box and carries the path
        # the session was run from — we use it as the project's display_name.
        wire_objs: list = []
        marker_items: list[tuple[str, str]] = []
        for obj in r2.list_keys():
            parts = obj.key.split("/")
            if len(parts) >= 4 and parts[0] == "sessions" \
                    and parts[-1] in ("wire.jsonl", "wire.jsonl.xz"):
                wire_objs.append(obj)
            elif len(parts) == 3 and parts[0] == "sessions" \
                    and parts[2] == "project.json":
                marker_items.append((parts[1], obj.key))

        workers = _worker_count()

        # Fetch marker bodies on the pool; project_paths must be fully
        # populated before the wire-object loop starts.
        project_paths: dict[str, str] = {}
        if marker_items:
            if workers == 1:
                marker_results = [
                    _fetch_marker(project_id, key)
                    for project_id, key in marker_items
                ]
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(_fetch_marker, project_id, key): (project_id, key)
                        for project_id, key in marker_items
                    }
                    marker_results = [f.result() for f in as_completed(futures)]
            for res in marker_results:
                if res is not None:
                    project_paths[res[0]] = res[1]

        seen_keys: set[str] = set()
        seen_projects: dict[str, dict] = {}
        # (obj, proj, project_id, session_id, is_main, stored) per file
        # needing work; fetched+parsed below on a pool.
        todo: list[tuple] = []

        for obj in wire_objs:
            parts = obj.key.split("/")
            project_id = parts[1]
            session_id = parts[2]
            # Subagent transcripts live at .../subagents/<sub-id>/wire.jsonl;
            # main session transcripts at .../<uuid>/wire.jsonl. Both get
            # ingested, but the classification drives the main/subagent
            # split surfaced by /api/dashboard.
            is_main = "/subagents/" not in obj.key
            listed += 1
            seen_keys.add(obj.key)

            proj = seen_projects.setdefault(project_id, {
                "project_id": project_id,
                "display_name": project_paths.get(project_id, project_id),
                "first_seen_at": obj.last_modified,
                "last_seen_at": obj.last_modified,
            })
            if obj.last_modified < proj["first_seen_at"]:
                proj["first_seen_at"] = obj.last_modified
            if obj.last_modified > proj["last_seen_at"]:
                proj["last_seen_at"] = obj.last_modified

            stored = existing.get(obj.key)
            need_reparse = (
                stored is None
                or stored[0] != obj.etag
                or stored[1] != parser_version
            )
            if not need_reparse:
                continue

            todo.append((obj, proj, project_id, session_id, is_main, stored))

        # Fetch + parse is ~88% of per-file wall time and is network-bound
        # (one R2 GET each), so it runs on a thread pool. Persistence stays
        # on this thread: the per-file transaction boundary, and therefore
        # ordering and failure semantics, are exactly as before. Work is
        # submitted in bounded chunks so an 8k-file reparse does not hold
        # every inflated blob in memory at once.
        chunk = max(1, workers * 4)
        for start in range(0, len(todo), chunk):
            batch = todo[start:start + chunk]
            if workers == 1:
                results = ((item, _fetch_and_parse(item[0].key)) for item in batch)
            else:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {
                        pool.submit(_fetch_and_parse, item[0].key): item
                        for item in batch
                    }
                    results = [
                        (futures[f], f.result()) for f in as_completed(futures)
                    ]
            for (obj, proj, project_id, session_id, is_main, stored), parsed in results:
                _persist(
                    obj, proj, project_id, session_id, is_main,
                    parsed, parser_version,
                )
                if stored is None:
                    inserted += 1
                else:
                    reparsed += 1

        # Unconditional project upsert — even when no files needed reparse,
        # the project marker's path may have changed (e.g. user renamed a
        # work_dir), and the per-file inner block above runs only on reparse.
        # Pushing every seen project here keeps display_name fresh.
        if seen_projects:
            with db.viz_conn() as c, c.cursor() as cur:
                cur.executemany(
                    "INSERT INTO projects (project_id, display_name, "
                    "first_seen_at, last_seen_at) "
                    "VALUES (%(project_id)s, %(display_name)s, "
                    "%(first_seen_at)s, %(last_seen_at)s) "
                    "ON CONFLICT (project_id) DO UPDATE SET "
                    "  display_name = EXCLUDED.display_name, "
                    "  first_seen_at = LEAST(projects.first_seen_at, "
                    "                        EXCLUDED.first_seen_at), "
                    "  last_seen_at = GREATEST(projects.last_seen_at, "
                    "                          EXCLUDED.last_seen_at)",
                    list(seen_projects.values()),
                )
                c.commit()

        # Orphan files
        with db.viz_conn() as c, c.cursor() as cur:
            if seen_keys:
                cur.execute(
                    "DELETE FROM files WHERE file_key != ALL(%s) RETURNING 1",
                    (list(seen_keys),),
                )
            else:
                cur.execute("DELETE FROM files RETURNING 1")
            deleted = len(cur.fetchall())
            c.commit()

    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"

    finished = datetime.now(timezone.utc)
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "UPDATE ingest_runs SET finished_at=%s, r2_listed=%s, "
            "reparsed=%s, inserted=%s, deleted=%s, error=%s WHERE id=%s",
            (finished, listed, reparsed, inserted, deleted, err, run_id),
        )
        c.commit()

    summary = {
        "id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "trigger": trigger,
        "r2_listed": listed,
        "inserted": inserted,
        "reparsed": reparsed,
        "deleted": deleted,
        "error": err,
    }
    if err is None:
        # Order matters: the rollup reads is_canonical.
        recompute_canonical()
        rebuild_rollup()
        rebuild_tool_rollup()
        rebuild_latency_rollup()

    # Data changed: mark the response cache stale, then notify connected
    # SSE clients so the dashboard re-fetches without a page reload.
    #
    # invalidate() rather than clear(): clearing would drop every user onto
    # the uncached path on every ingest, so the refetch that ingest_done
    # triggers would block on a cold query. Marking stale keeps the entries
    # servable — the refetch returns the previous numbers instantly and the
    # fresh ones land via the background refresh. Threadsafe: ingest runs in
    # a scheduler thread.
    if err is None and (inserted or reparsed or deleted):
        cache.response_cache.invalidate()
        events.broadcast_threadsafe("ingest_done", summary)

    if err is None:
        warm_common()
    return summary


def rebuild_tool_rollup() -> int:
    """Rebuild `tool_rollup` from tool_uses + files.

    No join to `records`: tool_uses.line_num and records.line_num are
    disjoint in Kimi wire.jsonl, so there is nothing to match on and no
    per-tool-call model to record.

    n_total counts every tool call (/api/tool-usage), n_rated only the
    settled ones (/api/tool-error-rate's denominator), so one table serves
    both without either having to approximate the other.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute("TRUNCATE tool_rollup")
        cur = c.execute(
            """
            INSERT INTO tool_rollup (
              hour, project_id, tool_name, n_total, n_rated, n_error
            )
            SELECT date_trunc('hour', tu.ts) AS hour,
                   f.project_id,
                   tu.tool_name,
                   COUNT(*)                                       AS n_total,
                   COUNT(*) FILTER (WHERE tu.is_error IS NOT NULL) AS n_rated,
                   COUNT(*) FILTER (WHERE tu.is_error)             AS n_error
              FROM tool_uses tu
              JOIN files f ON f.file_key = tu.file_key
             WHERE tu.ts IS NOT NULL
             GROUP BY 1, 2, 3
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_tool_rollup: %d rows", written)
    return written


# Display bucket widths /api/reply-latency can ask for, from
# api._bucket_seconds. The sub-hour widths (the 24h view) are deliberately
# absent: a row per 5 minutes of all history to serve one day is not worth
# it, and that range stays on the live path.
LATENCY_BUCKETS = (3600, 21600, 43200, 86400)


def rebuild_latency_rollup() -> int:
    """Rebuild `latency_rollup` for each display bucket width.

    Two passes per width — one grouped by project, one for the
    all-projects row (project_id = '') — because percentiles are not
    composable across a filter. Outlier dots are computed in the same
    pass and stored as JSONB.
    """
    written = 0
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '128MB'")
        c.execute("TRUNCATE latency_rollup")
        for bs in LATENCY_BUCKETS:
            for scope_expr, scope_join in (
                ("f.project_id", "JOIN files f ON f.file_key = r.file_key"),
                ("''", ""),
            ):
                cur = c.execute(
                    f"""
                    WITH src AS (
                      SELECT to_timestamp(
                               floor(EXTRACT(EPOCH FROM r.ts) / {bs}) * {bs} + {bs} / 2
                             ) AS bucket,
                             {scope_expr} AS project_id,
                             COALESCE(NULLIF(r.model, ''), 'unknown') AS model,
                             r.ts, r.file_key, r.line_num,
                             r.reply_latency_s AS latency_s
                        FROM records r
                        {scope_join}
                       WHERE r.reply_latency_s IS NOT NULL
                    ),
                    bands AS (
                      SELECT bucket, project_id, model, COUNT(*) AS n,
                             PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY latency_s) AS p10,
                             PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY latency_s) AS p50,
                             PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY latency_s) AS p90
                        FROM src GROUP BY 1, 2, 3
                    ),
                    ranked AS (
                      SELECT s.*,
                             b.n AS bucket_n,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s DESC) AS rn_high,
                             ROW_NUMBER() OVER (PARTITION BY s.bucket, s.project_id, s.model
                                                ORDER BY s.latency_s ASC)  AS rn_low
                        FROM src s
                        JOIN bands b USING (bucket, project_id, model)
                       WHERE b.n >= 100
                    ),
                    picked AS (
                      SELECT bucket, project_id, model,
                             jsonb_agg(jsonb_build_object(
                               'ts', ts, 'latency_s', latency_s,
                               'file_key', file_key, 'line_num', line_num
                             ) ORDER BY latency_s DESC) AS outliers
                        FROM ranked
                       -- GREATEST(1, CEIL(n * 0.01)), matching the live
                       -- query exactly. `n / 100` would floor instead, so
                       -- a bucket of 150 would yield 1 dot, not 2.
                       WHERE rn_high <= GREATEST(1, CEIL(bucket_n * 0.01))
                          OR rn_low  <= GREATEST(1, CEIL(bucket_n * 0.01))
                       GROUP BY 1, 2, 3
                    )
                    INSERT INTO latency_rollup
                      (bucket_s, bucket, project_id, model, n, p10, p50, p90, outliers)
                    SELECT {bs}, b.bucket, b.project_id, b.model, b.n,
                           b.p10, b.p50, b.p90, COALESCE(p.outliers, '[]'::jsonb)
                      FROM bands b
                      LEFT JOIN picked p USING (bucket, project_id, model)
                    """
                )
                written += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_latency_rollup: %d rows", written)
    return written


def warm_common() -> None:
    """Pre-populate the response cache for the views a fresh visitor hits.

    After an ingest the buffer cache is cold (the recompute and rollup
    rebuild just rewrote the tables) and after a RESTART the response
    cache is empty too, so the first load pays both.
    Stale-while-revalidate cannot cover the restart case because there is
    nothing stale to serve.

    Only the unfiltered default views are warmed. The full keyspace is
    (endpoint x range x project x model), which is far too large to
    precompute and would mostly evict itself; a project the user actually
    opens still costs one cold query, but the landing view never does.

    Runs on the cache's background pool, so ingest returns immediately.
    Disabled by KIMIMETER_WARM_CACHE=0 — the tests set that, because a
    warm outlives the fixture that created its database and its queries
    then race the teardown that drops it.
    """
    if os.environ.get("KIMIMETER_WARM_CACHE", "1").lower() in ("0", "false", "no"):
        return

    from backend import api

    ranges = ("all", "30d", "7d")
    for rng in ranges:
        cache.warm(api.dashboard, range=rng)
        cache.warm(api.activity_heatmap, range=rng)
        cache.warm(api.tool_usage, range=rng)
        cache.warm(api.tool_error_rate, range=rng)
        cache.warm(api.reply_latency, range=rng)
    cache.warm(api.list_projects)
    log.info("warm_common: queued %d range(s)", len(ranges))


def recompute_canonical() -> int:
    """Resolve cross-file uuid dedup into `records.is_canonical`.

    Marks exactly the row that ``DISTINCT ON (r.uuid) ... ORDER BY r.uuid,
    r.file_key`` used to pick at read time, so the read endpoints can
    filter on a boolean instead of sorting the whole table on every
    request. ``line_num`` breaks ties within a file_key, which the old
    read-time ORDER BY left arbitrary.

    Rows with a NULL uuid are legacy records kept verbatim (they were the
    UNION ALL leg), so they are always canonical.

    Runs after EVERY successful ingest, not only when files changed: a
    freshly-migrated DB has the column defaulted to TRUE across the board,
    and skipping the pass on a no-op ingest would leave duplicates
    double-counted until something happened to change. The UPDATE only
    touches rows whose flag actually flips, so a steady-state pass writes
    nothing. Returns the number of rows changed.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        cur = c.execute(
            """
            UPDATE records r
               SET is_canonical = w.canon
              FROM (
                    SELECT file_key, line_num,
                           (uuid IS NULL OR ROW_NUMBER() OVER (
                              PARTITION BY uuid ORDER BY file_key, line_num
                            ) = 1) AS canon
                      FROM records
                   ) w
             WHERE r.file_key = w.file_key
               AND r.line_num = w.line_num
               AND r.is_canonical IS DISTINCT FROM w.canon
            """
        )
        changed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    if changed:
        log.info("recompute_canonical: %d rows reflagged", changed)
    return changed


def rebuild_rollup() -> int:
    """Rebuild `usage_rollup` from the canonical records.

    Full rebuild rather than an incremental merge: cross-file uuid dedup
    means a newly ingested FILE can demote a record that another file
    already contributed, so touched-files-only would leave stale sums
    behind. The whole table is ~1 row per (session, hour, model), which is
    a fraction of `records`, so rebuilding it costs one pass.

    Must run AFTER recompute_canonical() — it reads is_canonical.
    Returns the row count written.
    """
    with db.viz_conn() as c:
        c.execute("SET LOCAL work_mem = '64MB'")
        c.execute("TRUNCATE usage_rollup")
        cur = c.execute(
            """
            INSERT INTO usage_rollup (
              session_id, project_id, hour, model, is_main,
              first_ts, last_ts, requests,
              fresh_tokens, output_tokens, cache_read_tokens, cost_usd
            )
            SELECT f.session_id,
                   f.project_id,
                   date_trunc('hour', r.ts)                    AS hour,
                   COALESCE(NULLIF(r.model, ''), 'unknown')    AS model,
                   f.is_main,
                   MIN(r.ts), MAX(r.ts), COUNT(*),
                   COALESCE(SUM(r.fresh_tokens), 0),
                   COALESCE(SUM(r.output_tokens), 0),
                   COALESCE(SUM(r.cache_read_tokens), 0),
                   COALESCE(SUM(r.cost_usd), 0)
              FROM records r
              JOIN files f ON f.file_key = r.file_key
             WHERE r.is_canonical AND r.ts IS NOT NULL
             GROUP BY 1, 2, 3, 4, 5
            """
        )
        written = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        c.commit()
    log.info("rebuild_rollup: %d rows", written)
    return written


def _worker_count() -> int:
    """Fetch+parse concurrency.

    Unset or unparseable -> auto (network-bound work, so oversubscribe
    cores). An explicit number is honoured, clamped to at least 1, so
    INGEST_WORKERS=1 is a real "go sequential" switch for debugging.
    """
    auto = min(16, (os.cpu_count() or 4) * 2)
    raw = os.environ.get("INGEST_WORKERS", "").strip()
    if not raw:
        return auto
    try:
        return max(1, int(raw))
    except ValueError:
        return auto


def _fetch_and_parse(key: str) -> dict:
    """Runs on a pool thread. Touches no DB connection."""
    return parse.parse_file(key, r2.get_object(key))


def _fetch_marker(project_id: str, key: str) -> tuple[str, str] | None:
    """Fetch and parse a sessions/<hash>/project.json marker on a pool thread.

    Runs on a pool thread and touches no DB connection.
    """
    try:
        blob = r2.get_object(key)
        data = json.loads(blob.decode("utf-8"))
        path = data.get("path")
        if isinstance(path, str) and path:
            return (project_id, path)
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return None


def _persist(obj, proj, project_id, session_id, is_main, parsed,
             parser_version) -> None:
    """One file, one transaction — identical to the pre-pool behaviour."""
    with db.viz_conn() as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO projects (project_id, display_name, "
            "first_seen_at, last_seen_at) "
            "VALUES (%(project_id)s, %(display_name)s, "
            "%(first_seen_at)s, %(last_seen_at)s) "
            "ON CONFLICT (project_id) DO UPDATE SET "
            "  display_name = EXCLUDED.display_name, "
            "  first_seen_at = LEAST(projects.first_seen_at, "
            "                        EXCLUDED.first_seen_at), "
            "  last_seen_at = GREATEST(projects.last_seen_at, "
            "                          EXCLUDED.last_seen_at)",
            proj,
        )
        cur.execute(
            "DELETE FROM records WHERE file_key = %s", (obj.key,)
        )
        cur.execute(
            """
            INSERT INTO files (file_key, project_id, session_id,
              is_main, r2_etag, r2_size_bytes, r2_last_modified,
              parsed_at, parser_version, ctx_turns, turn_count,
              rate_limit_hits)
            VALUES (%(file_key)s, %(project_id)s, %(session_id)s,
              %(is_main)s, %(r2_etag)s, %(r2_size_bytes)s,
              %(r2_last_modified)s, %(parsed_at)s, %(parser_version)s,
              %(ctx_turns)s::jsonb, %(turn_count)s,
              %(rate_limit_hits)s::jsonb)
            ON CONFLICT (file_key) DO UPDATE SET
              project_id = EXCLUDED.project_id,
              session_id = EXCLUDED.session_id,
              is_main = EXCLUDED.is_main,
              r2_etag = EXCLUDED.r2_etag,
              r2_size_bytes = EXCLUDED.r2_size_bytes,
              r2_last_modified = EXCLUDED.r2_last_modified,
              parsed_at = EXCLUDED.parsed_at,
              parser_version = EXCLUDED.parser_version,
              ctx_turns = EXCLUDED.ctx_turns,
              turn_count = EXCLUDED.turn_count,
              rate_limit_hits = EXCLUDED.rate_limit_hits
            """,
            {
                "file_key": obj.key,
                "project_id": project_id,
                "session_id": session_id,
                "is_main": is_main,
                "r2_etag": obj.etag,
                "r2_size_bytes": obj.size,
                "r2_last_modified": obj.last_modified,
                "parsed_at": datetime.now(timezone.utc),
                "parser_version": parser_version,
                "ctx_turns": json.dumps(parsed["ctx_turns"], default=str),
                "turn_count": parsed["turn_count"],
                "rate_limit_hits": json.dumps(
                    parsed.get("rate_limit_hits", []), default=str
                ),
            },
        )
        cur.execute(
            "DELETE FROM tool_uses WHERE file_key = %s", (obj.key,)
        )
        if parsed.get("tool_uses"):
            cur.executemany(
                """
                INSERT INTO tool_uses (file_key, line_num, idx, ts, tool_name, is_error)
                VALUES (%(file_key)s, %(line_num)s, %(idx)s, %(ts)s, %(tool_name)s, %(is_error)s)
                """,
                parsed["tool_uses"],
            )
        if parsed["records"]:
            cur.executemany(
                """
                INSERT INTO records (file_key, line_num, uuid,
                  ts, model, fresh_tokens,
                  cache_creation_tokens, cache_read_tokens,
                  output_tokens, cost_usd,
                  text_chars, reply_latency_s)
                VALUES (%(file_key)s, %(line_num)s, %(uuid)s,
                  %(ts)s, %(model)s,
                  %(fresh_tokens)s, %(cache_creation_tokens)s,
                  %(cache_read_tokens)s, %(output_tokens)s,
                  %(cost_usd)s,
                  %(text_chars)s, %(reply_latency_s)s)
                """,
                parsed["records"],
            )
        c.commit()
