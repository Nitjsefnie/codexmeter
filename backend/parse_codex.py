"""Codex rollout JSONL → per-file (records list + ctx_turns array).

~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl, one JSON object per
line, each {timestamp, type, payload}. `type` is one of session_meta,
turn_context, event_msg, response_item, world_state, compacted,
inter_agent_communication_metadata; event_msg and response_item carry a
second discriminator in payload.type.

Entry point is backend.parse.parse_file, which detects the format and calls
parse() here. The return shape is identical to the Kimi parsers' — see
parse_file's docstring, which is the contract for all three.

FOUR PROPERTIES MAKE THE OBVIOUS TOKEN SUM WRONG. Each is a measured
correction, not defensive coding. Figures are from the 51 rollout files on
this box on 2026-08-05, holding 30,249 token_count events; that corpus is
live, so the counts date rather than pin:

1. info.total_token_usage is a MONOTONIC CUMULATIVE counter, and a forked
   thread INHERITS its parent's running total. 8 of the 51 files open with
   43M-70M tokens already on the clock. Summing per-file finals inflates the
   corpus by 1.15x. _codex_token_count therefore differences consecutive
   snapshots and never trusts one total; the first snapshot's baseline is
   recovered as total - last, so the inherited head never enters a delta.
2. Duplicate token_count events repeat the previous last_token_usage
   verbatim — 1,360 of the 30,249 here, in runs of up to 4; the reference
   parser reports a run of 47 in its own corpus. Summing last_token_usage
   double-counts. Differencing drops them for free: a non-advancing
   snapshot yields a delta of zero, which is skipped.
3. cached_input_tokens is a SUBSET of input_tokens, not an addend, as is
   cache_write_input_tokens. Observed cache rates run 90-98%, so adding
   instead of subtracting overstates fresh input by ~40x.
4. token_count payloads carry NO model field and NO agent id, while one
   session mixes models and folds subagent usage into the same counter. The
   model comes from the most recent turn_context / thread_settings_applied
   record preceding the token_count line.

The logic is ported from ~/.agent-bundle/scripts/parse_codex.py, a
standalone CLI over the same format, validated against a 46-file corpus.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

import orjson

from backend import pricing
from backend.parse_common import (_append_tool_use, _append_usage_record,
                                  _end_turn, _finish_parse,
                                  _mark_assistant_event, _ParseState, _to_dt,
                                  _turn_boundary)

# The single custom tool is `exec`, whose input is a JS program calling
# tools.<api>({...}). The api is the useful tool name — `exec` alone would
# collapse every shell command, patch and plan update into one bucket.
_CODEX_API_RE = re.compile(r"tools\.([A-Za-z_][A-Za-z_0-9]*)\s*\(")

# Model in force -> canonical pricing label. Substring, not prefix: the corpus
# carries "gpt5.6-sol" (missing separator) alongside "gpt-5.6-sol". An
# unrecognised gpt-5.6-* variant bills at the FLAGSHIP rate, the opposite of
# the Kimi ladder's conservative fallback: here a wrong overcount is visible
# and arguable, while a wrong undercount silently understates the bill.
_CODEX_FLAGSHIP = "gpt-5.6-sol"
_CODEX_MODEL_MAP = (
    ("terra", "gpt-5.6-terra"),
    ("luna", "gpt-5.6-luna"),
    ("sol", "gpt-5.6-sol"),
)

# The cumulative counter's fields. All six are differenced so a duplicate
# snapshot is recognised by ALL of them failing to advance, not just by the
# four that are billed.
_CODEX_USAGE_KEYS = (
    "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
    "output_tokens", "reasoning_output_tokens", "total_tokens",
)

# A tool result whose first line starts with one of these is a failure. The
# payload carries no status field of its own.
_CODEX_FAILURE_HEADS = ("Script failed", "collab spawn failed")

# Every top-level record type the format emits; the format's fingerprint.
RECORD_TYPES = frozenset({
    "session_meta", "turn_context", "event_msg", "response_item",
    "world_state", "compacted", "inter_agent_communication_metadata",
})


def _codex_model(raw: str | None) -> str:
    """Model string in force -> canonical pricing label.

    Only canonical labels may reach pricing.compute_cost: an unmapped id
    resolves to DEFAULT_RATES, which are KIMI k2-6 rates — off by ~5x on
    fresh input for a Codex record, silently.
    """
    lowered = (raw or "").lower()
    for needle, label in _CODEX_MODEL_MAP:
        if needle in lowered:
            return label
    return _CODEX_FLAGSHIP


@dataclass
class _CodexState(_ParseState):
    """_ParseState plus the state only the Codex format needs."""
    # Previous cumulative token snapshot, for differencing. None until the
    # file's first token_count seeds the inherited baseline.
    prev_usage: dict | None = None
    # Model in force, from the most recent turn_context / settings record.
    model: str | None = None
    # The file's only declared model, when it declares exactly one. Attributes
    # the token_count records that precede the first turn_context.
    sole_model: str | None = None
    # (index into tool_uses, turn_id) of the most recent apply_patch call, to
    # carry a patch_apply_end's churn back onto the call that caused it.
    patch_target: tuple[int, str | None] | None = None
    # Last rate-limit condition booked, so a condition spanning hundreds of
    # token_count events books one hit rather than hundreds.
    last_rl_kind: str | None = None
    # This thread's session id, from session_meta. A fork REUSES its
    # parent's, which is what makes it the right half of a record's
    # cross-file identity — see _codex_record_uuid.
    session_id: str | None = None


def _codex_declared_models(blob: bytes) -> set[str]:
    """Every model this file declares, from a cheap pre-scan.

    A forked rollout replays its parent's history before the new thread emits
    a turn_context, so the leading token_count records have no model in front
    of them. Where the file declares exactly one model overall there is only
    one answer; where it mixes models the prefix stays on the fallback rather
    than guessing which side of the switch it belonged to.

    The scan JSON-decodes only lines that mention a model-declaring record —
    on a 20MB rollout that is a few hundred lines out of tens of thousands.
    """
    models: set[str] = set()
    for raw in blob.splitlines():
        if b'"turn_context"' not in raw and b'"thread_settings_applied"' not in raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "turn_context":
            name = payload.get("model")
        elif payload.get("type") == "thread_settings_applied":
            name = (payload.get("thread_settings") or {}).get("model")
        else:
            continue
        if name:
            models.add(str(name))
    return models


def _codex_output_text(payload: dict) -> str:
    """Flatten a *_call_output payload into one string."""
    out = payload.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for chunk in out:
            if isinstance(chunk, dict) and chunk.get("text"):
                parts.append(str(chunk["text"]))
            elif isinstance(chunk, str):
                parts.append(chunk)
        return "\n".join(parts)
    return ""


def _diff_churn(diff: object) -> tuple[int, int]:
    """(lines_added, lines_deleted) for one unified diff.

    Unlike the Kimi formats, whose tool results carry no diff and whose churn
    has to be inferred from the CALL's arguments, Codex journals the applied
    unified diff in patch_apply_end — so this counts what was actually
    written, not what was requested.
    """
    added = deleted = 0
    for line in str(diff or "").splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            deleted += 1
    return added, deleted


def _codex_record_uuid(st: _CodexState, cumulative: dict,
                       line_num: int) -> str:
    """Cross-file identity for ONE request.

    THE FIFTH TRAP, and the one that only bites once records from several
    files are summed. A resumed or forked rollout REPLAYS its parent's
    history into its own file, so the same request is journalled in many
    files: across the 51 local rollouts, 28,889 per-file records represent
    only 12,945 distinct requests, and one session id spans 39 files.
    Aggregating those per-file rows without dedup over-counts by ~2.2x.

    kimimeter already solved this — `records.uuid` plus `is_canonical`,
    resolved by ingest.recompute_canonical(), which keeps exactly one row
    per uuid. What that mechanism needs from a parser is a uuid that is
    IDENTICAL across every file replaying a given request, which
    "<file_key>:<line>" can never be.

    A request is identified by the position it advanced its thread's
    cumulative counter to: the counter is monotonic within a thread, and a
    replay reproduces the same readings. Measured over the local corpus,
    3,806 keys repeat across files and exactly ONE of them carries
    conflicting usage, so collapsing them is safe to ~0.03%.

    Falls back to the per-file identity when the file declares no
    session_meta — a mid-file window rather than a whole rollout. Such a
    fragment has no thread identity to share, so per-file is the honest
    answer; it just cannot dedup against anything.
    """
    if not st.session_id:
        return f"{st.file_key}:{line_num}"
    return f"{st.session_id}:{cumulative['total_tokens']}"


def _codex_token_count(st: _CodexState, line_num: int, ts: datetime | None,
                       payload: dict) -> None:
    """Book ONE billing record per real request (traps 1-4)."""
    info = payload.get("info") or {}
    total = info.get("total_token_usage") or {}
    if not total:
        return
    cumulative = {k: int(total.get(k) or 0) for k in _CODEX_USAGE_KEYS}
    if st.prev_usage is None:
        # Trap 1. This file's inherited baseline is its first cumulative
        # snapshot minus the request that produced it, so the first delta is
        # that request alone and the parent's millions never enter a sum.
        last = info.get("last_token_usage") or {}
        st.prev_usage = {k: cumulative[k] - int(last.get(k) or 0)
                         for k in _CODEX_USAGE_KEYS}
    delta = {k: cumulative[k] - st.prev_usage[k] for k in _CODEX_USAGE_KEYS}
    st.prev_usage = cumulative
    if all(delta[k] <= 0 for k in _CODEX_USAGE_KEYS):
        return  # Trap 2: duplicate token_count, or a non-advancing snapshot.

    # Trap 3: cached and cache-write inputs are SUBSETS of input_tokens.
    # cache_write is its own billed bucket, NOT part of fresh: Codex charges
    # a cache write at 1.25x uncached input, so folding it into fresh
    # underbills it and folding it into cached overbills by 10x. It reads 0
    # across the whole local corpus, which is a property of the models that
    # corpus used and not of the format — the field is real and billable
    # (codex-rs/protocol/src/protocol.rs declares and sums it), so it is
    # counted and carried on its own.
    total_in = max(0, delta["input_tokens"])
    read = max(0, delta["cached_input_tokens"])
    create = max(0, delta["cache_write_input_tokens"])
    fresh = max(0, total_in - read - create)
    output = max(0, delta["output_tokens"])
    # Subset of output, not an addend — carried, never added to cost.
    reasoning = max(0, delta["reasoning_output_tokens"])

    # Trap 4: the payload names no model; the surrounding turn_context does.
    _append_usage_record(
        st, line_num, ts,
        _codex_record_uuid(st, cumulative, line_num),
        _codex_model(st.model or st.sole_model),
        (fresh, create, read, output),
        reasoning=reasoning,
        long_context=total_in > pricing.LONG_CONTEXT_THRESHOLD,
    )


def _codex_rate_limit(st: _CodexState, line_num: int, ts: datetime | None,
                      payload: dict) -> None:
    """Book a rate-limit hit, if this token_count reports one.

    UNVERIFIED AGAINST REAL DATA: rate_limit_reached_type and
    spend_control_reached are present on all 30,249 token_count payloads in
    the local corpus and null on every one of them (peak primary utilisation
    28%), so no hit has ever been observed. The fields are read by name; the
    VALUE a hit carries is unknown, so any non-null is booked and rendered
    with str(). A condition holds across many consecutive token_count events,
    so consecutive repeats collapse into one hit.
    """
    rl = payload.get("rate_limits") or {}
    kind = rl.get("rate_limit_reached_type")
    if not kind and not rl.get("spend_control_reached"):
        st.last_rl_kind = None
        return
    key = str(kind) if kind else "spend_control_reached"
    if key == st.last_rl_kind:
        return
    st.last_rl_kind = key
    primary = rl.get("primary") or {}
    st.rate_limit_hits.append({
        "line": line_num,
        "ts": ts.isoformat() if ts is not None else "",
        "content": (
            f"{key} (plan={rl.get('plan_type')}, "
            f"primary {primary.get('used_percent')}% of "
            f"{primary.get('window_minutes')}m window)"
        )[:500],
    })


def _codex_tool_call(st: _CodexState, line_num: int, ts: datetime | None,
                     payload: dict) -> None:
    """Record one tool call.

    A custom_tool_call is always `exec`, whose real identity is the first
    tools.<api> its JS program calls — exec_command, apply_patch, write_stdin,
    update_plan. A function_call names its function directly (spawn_agent,
    wait_agent, send_message, ...).
    """
    if payload.get("type") == "custom_tool_call":
        apis = _CODEX_API_RE.findall(payload.get("input") or "")
        name = apis[0] if apis else str(payload.get("name") or "")
    else:
        apis = [str(payload.get("name") or "")]
        name = apis[0]
    _append_tool_use(st, line_num, ts, name, str(payload.get("call_id") or ""))
    if "apply_patch" in apis:
        turn_id = (payload.get("internal_chat_message_metadata_passthrough")
                   or {}).get("turn_id")
        st.patch_target = (len(st.tool_uses) - 1, turn_id)


def _codex_tool_result(st: _CodexState, payload: dict) -> None:
    """Settle one tool call's is_error.

    The payload carries no status field; failure is announced in the first
    line of the output text. A backgrounded script ("Script running with cell
    ID ...") has not failed — it has not finished.
    """
    call_id = payload.get("call_id")
    if not call_id:
        return
    text = _codex_output_text(payload)
    head = text.split("\n", 1)[0] if text else ""
    st.tool_result_is_error[str(call_id)] = head.startswith(
        _CODEX_FAILURE_HEADS
    )


def _codex_patch(st: _CodexState, line_num: int, ts: datetime | None,
                 payload: dict) -> None:
    """Attribute one patch_apply_end's line churn.

    patch_apply_end does NOT share a call_id namespace with the tool calls
    (0 of 1,727 patch call_ids match one — patches carry "exec-<uuid>" ids),
    so the churn is carried back onto the most recent apply_patch call of the
    SAME turn. One exec can apply several patches; they sum onto that call.

    Where no such call exists in this file the patch came from a subagent,
    whose tool calls the parent rollout does not record (522 of the 524
    unattributable patches in the local corpus sit in files with
    sub_agent_activity). Those get a row of their own rather than being
    dropped: the work happened, and this file is the only record of it.

    A failed patch changed nothing, so it contributes no churn — the same
    rule _resolve_tool_errors applies to a rejected edit.
    """
    ok = bool(payload.get("success"))
    added = deleted = 0
    if ok:
        for change in (payload.get("changes") or {}).values():
            if isinstance(change, dict):
                a, d = _diff_churn(change.get("unified_diff"))
                added += a
                deleted += d
    target = st.patch_target
    if target is not None and target[1] == payload.get("turn_id"):
        tool_use = st.tool_uses[target[0]]
        tool_use["lines_added"] += added
        tool_use["lines_deleted"] += deleted
        return
    # No tool_call_id: _resolve_tool_errors must leave this row's is_error,
    # which is settled here and nowhere else, alone.
    _append_tool_use(st, line_num, ts, "apply_patch", "", (added, deleted))
    st.tool_uses[-1]["is_error"] = not ok


def _codex_event_msg(st: _CodexState, ptype: str, line_num: int,
                     ts: datetime | None, payload: dict) -> None:
    if ptype == "token_count":
        _codex_rate_limit(st, line_num, ts, payload)
        _codex_token_count(st, line_num, ts, payload)
    elif ptype == "task_started":
        _turn_boundary(st, line_num, ts)
    elif ptype in ("task_complete", "turn_aborted"):
        _end_turn(st, line_num, ts)
    elif ptype == "agent_message":
        # event_msg/agent_message is a SUPERSET of response_item/message with
        # role=assistant (every one of 496 distinct assistant texts sampled
        # appears in both), so only this side is counted.
        st.text_chars_since_turn += len(str(payload.get("message") or ""))
        _mark_assistant_event(st)
    elif ptype == "patch_apply_end":
        _codex_patch(st, line_num, ts, payload)
    elif ptype == "thread_settings_applied":
        model = (payload.get("thread_settings") or {}).get("model")
        if model:
            st.model = str(model)


def _codex_response_item(st: _CodexState, ptype: str, line_num: int,
                         ts: datetime | None, payload: dict) -> None:
    if ptype in ("custom_tool_call", "function_call"):
        _codex_tool_call(st, line_num, ts, payload)
        _mark_assistant_event(st)
    elif ptype in ("custom_tool_call_output", "function_call_output"):
        _codex_tool_result(st, payload)


def _codex_dispatch(st: _CodexState, rtype: str, line_num: int,
                    ts: datetime | None, payload: dict) -> None:
    if rtype == "event_msg":
        _codex_event_msg(st, str(payload.get("type") or ""),
                         line_num, ts, payload)
    elif rtype == "response_item":
        _codex_response_item(st, str(payload.get("type") or ""),
                             line_num, ts, payload)
    elif rtype == "turn_context":
        model = payload.get("model")
        if model:
            st.model = str(model)
    elif rtype == "session_meta":
        # First one wins: a rollout declares its own thread once, at the
        # head. Anything later belongs to a replayed parent.
        if st.session_id is None and payload.get("session_id"):
            st.session_id = str(payload["session_id"])
    # world_state / compacted / inter_agent_communication_metadata: no
    # billing or tool consequence.
    #
    # mcp_tool_call_end and web_search_end are deliberately NOT tool_uses
    # rows. They share no call_id with the tool calls (0 of 324 and 0 of 11),
    # but every file that makes MCP calls through exec shows the two counts
    # matching exactly (11/11, 4/4) — they are the server side of the same
    # call, and a row each would count it twice.


def parse(file_key: str, blob: bytes) -> dict:
    """Parse one Codex rollout JSONL. Same return shape as _parse_legacy."""
    lines = blob.splitlines()
    declared = _codex_declared_models(blob)
    st = _CodexState(file_key)
    st.sole_model = next(iter(declared)) if len(declared) == 1 else None

    for line_num, raw in enumerate(lines, 1):
        if not raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        ts_dt = _to_dt(obj.get("timestamp"))
        if ts_dt is not None and st.first_event_ts is None:
            st.first_event_ts = ts_dt

        _codex_dispatch(st, str(obj.get("type") or ""), line_num, ts_dt, payload)

    return _finish_parse(st, len(lines))
