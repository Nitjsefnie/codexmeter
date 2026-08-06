/**
 * In-browser wire.jsonl parser for the Inspector.
 * Mirrors backend/parse.py and ~/.kimi-code/scripts/parse_wire.py.
 */

// Must match backend PARSER_VERSION (see backend/.env.example).
window.PARSER_VERSION = "8";

const MODEL_RATES = {
  "kimi-k3":        { fresh: 3.00, create: 0.00, read: 0.30, output: 15.00 },
  "kimi-k2-7-code": { fresh: 0.95, create: 0.00, read: 0.19, output: 4.00 },
  "kimi-k2-6":      { fresh: 0.95, create: 0.00, read: 0.16, output: 4.00 },
};

const DEFAULT_RATES = MODEL_RATES["kimi-k2-6"];

// Hardcoded model transitions, oldest first.  Each constant is a frozen UTC
// epoch, NOT a live expression.  Boundaries are strictly-before /
// inclusive-at, so each cutoff instant belongs to the NEWER model.
// MODEL_CUTOFF applies to any record the wire did not settle; K3_CUTOFF
// applies ONLY to records with no wire model string at all.
const MODEL_CUTOFF_EPOCH = 1781217035;  // 2026-06-11 22:30:35 UTC
// 2026-07-16 14:45:55 UTC — the earliest observed k3 usage.record. MUST match
// backend/parse.py K3_CUTOFF_EPOCH; the previous value here (1784214394,
// 15:06:34) postdated real k3 usage and had drifted 20m39s from the backend.
const K3_CUTOFF_EPOCH = 1784213155;

function tsToEpoch(ts) {
  if (ts == null) return null;
  if (typeof ts === "number") return ts;
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d.getTime() / 1000;
}

// Raw provider id -> canonical pricing label. Mirrors backend/parse.py
// _WIRE_MODEL_MAP. Only ids that identify a pricing model on their own belong
// here; "kimi-for-coding" spans both k2 generations and deliberately does not.
const WIRE_MODEL_MAP = { "k3": "kimi-k3" };

// Mirrors backend/parse.py _canonical_model. Returns null when the wire cannot
// settle the model alone. Canonicalising BEFORE any rate lookup is mandatory:
// rateForModel anchors at the start of the id, so a raw "kimi-code/k3" matches
// no key and bills at DEFAULT_RATES (k2-6) — a ~3x undercount.
function canonicalModel(wireModel) {
  if (!wireModel) return null;
  const tail = String(wireModel).split("/").pop();
  return WIRE_MODEL_MAP[tail] || null;
}

// Mirrors backend/parse.py _model_for. Per-RECORD, not per-session: a session
// can switch model mid-flight. Only ever returns one of the three canonical
// labels — anything else means the ladder is broken.
function modelFor(wireModel, ts) {
  const canonical = canonicalModel(wireModel);
  if (canonical !== null) return canonical;

  const epoch = tsToEpoch(ts);
  // An unstamped record is already-ingested history, so it cannot postdate the
  // K3 cutoff, and K3's rates are ~3x higher.
  if (epoch == null) return "kimi-k2-7-code";

  if (epoch < MODEL_CUTOFF_EPOCH) return "kimi-k2-6";

  // The wire named a model and it is not k3 (canonicalModel would have caught
  // it), so the date may only separate the k2 generations — never promote an
  // unrecognized id to k3.
  if (wireModel) return "kimi-k2-7-code";

  if (epoch < K3_CUTOFF_EPOCH) return "kimi-k2-7-code";
  return "kimi-k3";
}

// Mirrors backend/pricing.py _normalise: case-fold and normalise version
// separators, so "Kimi-K2.7-code" and "kimi-k2-7-code" name the same model.
// Deliberately does NOT strip a provider prefix — a raw wire id that reaches
// pricing ("kimi-code/k3") is a bug and must keep resolving to the default
// rather than being quietly repaired here.
function _normaliseModel(model) {
  return (model ? String(model) : "").trim().toLowerCase().replace(/\./g, "-");
}

// Mirrors backend/pricing.py _SNAPSHOT_SUFFIX. A dated snapshot suffix
// ("-20260601") names the same model at a pinned revision; a version suffix
// ("-8") or a mode suffix ("-turbo") names a DIFFERENT model, which the table
// cannot price.
const _SNAPSHOT_SUFFIX = /^-?\d{6,8}$/;

// Mirrors backend/pricing.py _match_key. ANCHORED at the start, with a
// remainder of nothing, a bracket, an "@" or a dated snapshot — that is the
// whole point: an unanchored substring test bills a model the table has never
// heard of at the rates of whatever key happens to sit inside its name, so
// "kimi-k3-mini" would take K3's ~3x rates on the strength of its name alone.
// No key here is a prefix of another, but the table stays most-specific-first
// so that a future one can be.
function _matchRateKey(norm) {
  for (const key of Object.keys(MODEL_RATES)) {
    if (!norm.startsWith(key)) continue;
    const rest = norm.slice(key.length);
    if (rest === "" || rest[0] === "[" || rest[0] === "@"
        || _SNAPSHOT_SUFFIX.test(rest)) {
      return key;
    }
  }
  return null;
}

// Rates for a model id, in the shape the views want: the backend's four
// buckets plus the claudit-compatible aliases (out/c5/c1h) app.jsx reads.
// An id the table cannot match is priced at DEFAULT_RATES — the same
// fall-back backend/pricing.py resolve() reports as kind="default".
window.rateForModel = function rateForModel(model) {
  const key = _matchRateKey(_normaliseModel(model));
  const r = key === null ? DEFAULT_RATES : MODEL_RATES[key];
  return {
    fresh: r.fresh, create: r.create, read: r.read, output: r.output,
    out: r.output, c5: r.create, c1h: 0,
  };
};

function parseTimestamp(ts) {
  if (!ts) return null;
  if (typeof ts === "number") return new Date(ts * 1000);
  const d = new Date(ts);
  return isNaN(d.getTime()) ? null : d;
}

function _iterJsonObjects(line) {
  const out = [];
  let pos = 0;
  const n = line.length;
  while (pos < n) {
    while (pos < n && /\s/.test(line[pos])) pos++;
    if (pos >= n) break;
    try {
      const obj = JSON.parse(line.slice(pos));
      out.push(obj);
      break;
    } catch (e) {
      const nextOpen = line.indexOf('{"', pos + 1);
      if (nextOpen === -1) break;
      pos = nextOpen;
    }
  }
  return out;
}

function _detectFormat(blob) {
  const lines = blob.split(/\r?\n/);
  for (const line of lines) {
    if (!line) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (!obj || typeof obj !== "object") continue;
    if (obj.type === "metadata") {
      return obj.created_at != null ? "kimi-code" : "legacy";
    }
    if (["context.append_message", "context.append_loop_event", "usage.record", "turn.prompt", "turn.steer"].includes(obj.type)) {
      return "kimi-code";
    }
    if (obj.timestamp && ["StatusUpdate", "TurnBegin", "ToolCall", "ContentPart"].includes((obj.message || {}).type)) {
      return "legacy";
    }
  }
  return "legacy";
}

function _parseLegacy(blob) {
  const lines = blob.split(/\r?\n/);
  const events = [];
  const metaEvents = [];
  const toolCallMap = {};
  const toolResultMap = {};

  let currentTurn = null;
  let textCharsSinceTurn = 0;
  let pendingTurnBeginTs = null;
  let turnHasAssistantEvent = false;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    if (line.startsWith('{"type": "metadata"')) continue;

    let objs;
    try {
      objs = [JSON.parse(line)];
    } catch {
      objs = _iterJsonObjects(line);
      if (!objs.length) continue;
    }

    for (const obj of objs) {
      const ts = obj.timestamp;
      const msg = obj.message || {};
      const msgType = msg.type || "";
      const payload = msg.payload || {};
      const lineNum = i + 1;

      if (msgType === "TurnBegin") {
        if (currentTurn) {
          events.push({ type: "turn_end", ts: ts, line: lineNum });
        }
        let userInput = payload.user_input || "";
        if (Array.isArray(userInput)) {
          userInput = userInput.map(p => (typeof p === "object" ? p.text || "" : "")).join(" ");
        }
        events.push({ type: "user_message", ts, line: lineNum, detail: String(userInput) });
        metaEvents.push({ type: "turn_begin", ts, line: lineNum, raw: obj });
        currentTurn = { beginLine: lineNum, beginTs: ts, statusLines: [] };
        pendingTurnBeginTs = parseTimestamp(ts);
        turnHasAssistantEvent = false;
        textCharsSinceTurn = 0;
        continue;
      }

      if (msgType === "TurnEnd") {
        if (currentTurn) {
          currentTurn.endLine = lineNum;
          currentTurn.endTs = ts;
        }
        metaEvents.push({ type: "turn_end", ts, line: lineNum, raw: obj });
        currentTurn = null;
        pendingTurnBeginTs = null;
        continue;
      }

      if (msgType === "ContentPart") {
        const partType = payload.type;
        if (partType === "text") {
          textCharsSinceTurn += String(payload.text || "").length;
          events.push({ type: "assistant_text", ts, line: lineNum, detail: String(payload.text || "") });
        } else if (partType === "think") {
          events.push({ type: "thinking", ts, line: lineNum, detail: String(payload.think || "") });
        }
        if (!turnHasAssistantEvent && pendingTurnBeginTs) {
          turnHasAssistantEvent = true;
        }
        continue;
      }

      if (msgType === "ToolCall") {
        const func = payload.function || {};
        const toolName = func.name || "";
        let toolInput = {};
        try {
          toolInput = JSON.parse(func.arguments || "{}");
        } catch {
          toolInput = { _raw: func.arguments };
        }
        const tcId = payload.id || "";
        events.push({
          type: "tool_call", ts, line: lineNum,
          tool_name: toolName, tool_input: toolInput, tool_call_id: tcId,
        });
        if (tcId) toolCallMap[tcId] = events[events.length - 1];
        if (!turnHasAssistantEvent && pendingTurnBeginTs) {
          turnHasAssistantEvent = true;
        }
        continue;
      }

      if (msgType === "ToolResult") {
        const rv = payload.return_value || {};
        const tcId = payload.tool_call_id || "";
        let detail = "";
        if (typeof rv === "object") {
          const output = rv.output;
          if (Array.isArray(output)) {
            detail = output.map(x => (typeof x === "object" ? x.text || "" : String(x))).join("\n");
          } else {
            detail = String(output || "");
          }
        }
        const isError = (typeof rv === "object" && rv.is_error) || false;
        events.push({
          type: "tool_result", ts, line: lineNum,
          tool_call_id: tcId, is_error: isError, detail,
        });
        if (tcId) toolResultMap[tcId] = isError;
        continue;
      }

      if (msgType === "StatusUpdate") {
        const tu = payload.token_usage || {};
        metaEvents.push({
          type: "status_update", ts, line: lineNum,
          token_usage: tu,
          message_id: payload.message_id,
          context_tokens: payload.context_tokens,
          raw: obj,
        });
        if (currentTurn) {
          currentTurn.statusLines.push(lineNum);
        }
        continue;
      }

      // Catch-all
      metaEvents.push({ type: msgType.toLowerCase() || "unknown", ts, line: lineNum, raw: obj });
    }
  }

  // Link pairs
  for (const e of events) {
    if (e.type === "tool_result" && e.tool_call_id && toolCallMap[e.tool_call_id]) {
      e.paired_call = toolCallMap[e.tool_call_id];
      toolCallMap[e.tool_call_id].paired_result = e;
      e.is_error = toolResultMap[e.tool_call_id] || false;
    }
  }

  // Only a per-record fallback now: a record with no timestamp of its own
  // borrows the session's first event rather than going unlabelled.
  let firstEventTs = null;
  for (const e of events) {
    if (e.ts != null) { firstEventTs = e.ts; break; }
  }
  if (firstEventTs == null) {
    for (const m of metaEvents) {
      if (m.ts != null) { firstEventTs = m.ts; break; }
    }
  }
  // Build records from status_updates
  const records = [];
  let prevInput = 0;
  const ctxTurns = [];
  let turnIdx = 0;

  for (const m of metaEvents) {
    if (m.type !== "status_update") continue;
    const tu = m.token_usage || {};
    const fresh = tu.input_other || 0;
    const create = tu.input_cache_creation || 0;
    const read = tu.input_cache_read || 0;
    const output = tu.output || 0;
    const totalInput = fresh + create + read;

    // Legacy transcripts carry no model string, so this resolves by date —
    // but per RECORD, so a session spanning a cutoff splits correctly.
    const model = modelFor(null, m.ts != null ? m.ts : firstEventTs);
    const rates = window.rateForModel(model);
    const cost = (
      fresh * rates.fresh / 1e6 +
      create * rates.create / 1e6 +
      read * rates.read / 1e6 +
      output * rates.output / 1e6
    );

    records.push({
      line_num: m.line,
      uuid: m.message_id || null,
      ts: m.ts,
      model,
      fresh_tokens: fresh,
      cache_creation_tokens: create,
      cache_read_tokens: read,
      output_tokens: output,
      cost_usd: cost,
      text_chars: textCharsSinceTurn,
    });

    if (totalInput > 0) {
      turnIdx++;
      ctxTurns.push({
        idx: turnIdx,
        ts: m.ts ? new Date(m.ts * 1000).toISOString() : "",
        line: m.line,
        input: totalInput,
        output: output,
        delta: totalInput - prevInput,
      });
      prevInput = totalInput;
    }
  }

  return {
    records,
    ctx_turns: ctxTurns,
    turn_count: ctxTurns.length,
    events,
    meta_events: metaEvents,
  };
}

function _kcParseToolCall(tc) {
  if (tc.type !== "function") return { name: "", args: null, id: "" };
  const id = tc.id || "";
  if ("name" in tc) {
    return { name: tc.name || "", args: tc.arguments, id };
  }
  const fn = tc.function || {};
  return { name: fn.name || "", args: fn.arguments, id };
}

function _kcArgsToInput(args) {
  if (args == null) return {};
  if (typeof args === "object") return args;
  if (typeof args === "string") {
    try { return args ? JSON.parse(args) : {}; }
    catch { return { _raw: args }; }
  }
  return { _raw: args };
}

function _kcResultDetail(result) {
  if (!result || typeof result !== "object") return String(result || "");
  const output = result.output;
  if (Array.isArray(output)) {
    return output.map(x => (typeof x === "object" ? x.text || "" : String(x))).join("\n");
  }
  return String(output || "");
}

function _parseKimiCode(blob) {
  const lines = blob.split(/\r?\n/);
  const events = [];
  const metaEvents = [];
  const toolUses = [];
  const toolResultIsError = {};

  let currentTurn = null;
  let currentTurnId = null;
  let textCharsSinceTurn = 0;
  let pendingTurnBeginTs = null;

  function closeTurn(lineNum, ts) {
    if (currentTurn) {
      currentTurn.endLine = lineNum;
      currentTurn.endTs = ts;
      metaEvents.push({ type: "turn_end", ts, line: lineNum, raw: {} });
      currentTurn = null;
    }
  }
  function startTurn(lineNum, ts) {
    currentTurn = { beginLine: lineNum, beginTs: ts, statusLines: [] };
    textCharsSinceTurn = 0;
  }
  function countText(content) {
    let n = 0;
    for (const p of (content || [])) {
      if (p && p.type === "text") n += String(p.text || "").length;
    }
    return n;
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    let obj;
    try {
      obj = JSON.parse(line);
    } catch {
      continue;
    }
    if (!obj || typeof obj !== "object") continue;

    const typ = obj.type;
    const lineNum = i + 1;

    if (typ === "metadata") {
      continue;
    }

    const tsMs = obj.time;
    const ts = tsMs != null ? tsMs / 1000 : null;

    if (typ === "turn.prompt" || typ === "turn.steer") {
      closeTurn(lineNum, ts);
      startTurn(lineNum, ts);
      pendingTurnBeginTs = ts != null ? new Date(ts * 1000) : null;
      const inputParts = obj.input || [];
      const text = inputParts.map(p => (p && p.type === "text" ? p.text || "" : "")).join("");
      events.push({ type: typ === "turn.steer" ? "steer_input" : "user_message", ts, line: lineNum, detail: text });
      continue;
    }

    if (typ === "context.append_message") {
      const msg = obj.message || {};
      const role = msg.role;
      const content = msg.content || [];
      if (role === "assistant") {
        textCharsSinceTurn += countText(content);
        for (const p of content) {
          if (!p) continue;
          if (p.type === "text") events.push({ type: "assistant_text", ts, line: lineNum, detail: String(p.text || "") });
          else if (p.type === "think") events.push({ type: "thinking", ts, line: lineNum, detail: String(p.think || "") });
        }
        const msgToolCalls = msg.toolCalls || [];
        for (let tcIdx = 0; tcIdx < msgToolCalls.length; tcIdx++) {
          const { name, args, id } = _kcParseToolCall(msgToolCalls[tcIdx]);
          const toolInput = _kcArgsToInput(args);
          const callEvent = { type: "tool_call", ts, line: lineNum, tool_name: name, tool_input: toolInput, tool_call_id: id, detail: "" };
          // The wire's parallel-call representation: one assistant message
          // carrying several toolCalls. Annotate so the timeline badge and
          // computeSessionStats' parallel-batches count see real data.
          if (msgToolCalls.length > 1) {
            callEvent.batch_size = msgToolCalls.length;
            callEvent.batch_index = tcIdx + 1;
          }
          events.push(callEvent);
          toolUses.push({
            file_key: "", line_num: lineNum, idx: toolUses.length,
            ts: ts, tool_name: name, tool_call_id: id, is_error: null,
          });
        }
      } else if (role === "tool") {
        const tcId = msg.toolCallId;
        const detail = content.map(p => (p && p.type === "text" ? p.text || "" : "")).join("");
        const isErr = !!msg.isError;
        events.push({ type: "tool_result", ts, line: lineNum, tool_call_id: tcId || "", is_error: isErr, detail, message: "" });
        if (tcId) toolResultIsError[tcId] = isErr;
      } else if (role === "user") {
        const text = content.map(p => (p && p.type === "text" ? p.text || "" : "")).join("");
        events.push({ type: "user_message", ts, line: lineNum, detail: text });
      }
      continue;
    }

    if (typ === "context.append_loop_event") {
      const ev = obj.event || {};
      const et = ev.type;
      const turnId = ev.turnId;

      if (et === "step.begin") {
        if (turnId !== currentTurnId) {
          closeTurn(lineNum, ts);
          currentTurnId = turnId;
          startTurn(lineNum, ts);
        }
        metaEvents.push({ type: "step_begin", ts, line: lineNum, raw: ev });
        continue;
      }

      if (et === "step.end") {
        closeTurn(lineNum, ts);
        continue;
      }

      if (et === "content.part") {
        const part = ev.part || {};
        if (part.type === "text") {
          textCharsSinceTurn += String(part.text || "").length;
          events.push({ type: "assistant_text", ts, line: lineNum, detail: String(part.text || "") });
        } else if (part.type === "think") {
          events.push({ type: "thinking", ts, line: lineNum, detail: String(part.think || "") });
        }
        continue;
      }

      if (et === "tool.call") {
        const toolInput = _kcArgsToInput(ev.args);
        events.push({ type: "tool_call", ts, line: lineNum, tool_name: ev.name || "", tool_input: toolInput, tool_call_id: ev.toolCallId || "", detail: "" });
        toolUses.push({
          file_key: "", line_num: lineNum, idx: toolUses.length,
          ts: ts, tool_name: ev.name || "", tool_call_id: ev.toolCallId || "", is_error: null,
        });
        continue;
      }

      if (et === "tool.result") {
        const tcId = ev.toolCallId;
        const res = ev.result || {};
        const isErr = !!(res.isError || res.is_error);
        const detail = _kcResultDetail(res);
        events.push({ type: "tool_result", ts, line: lineNum, tool_call_id: tcId || "", is_error: isErr, detail, message: "" });
        if (tcId) toolResultIsError[tcId] = isErr;
        continue;
      }

      continue;
    }

    if (typ === "usage.record") {
      const usage = obj.usage || {};
      // The RAW provider id ("kimi-code/k3"). Never price or label with this
      // directly — it is canonicalised per record below.
      const wireModel = obj.model || null;
      const fresh = usage.inputOther || 0;
      const create = usage.inputCacheCreation || 0;
      const read = usage.inputCacheRead || 0;
      const output = usage.output || 0;

      let replyLatencyS = null;
      if (pendingTurnBeginTs && ts != null) {
        const delta = (new Date(ts * 1000) - pendingTurnBeginTs) / 1000;
        if (delta >= 0) replyLatencyS = delta;
      }
      pendingTurnBeginTs = null;

      metaEvents.push({
        type: "status_update", ts, line: lineNum,
        token_usage: {
          input_other: fresh,
          input_cache_creation: create,
          input_cache_read: read,
          output: output,
        },
        wire_model: wireModel,
        reply_latency_s: replyLatencyS,
        raw: obj,
      });

      if (currentTurn) {
        currentTurn.statusLines.push(lineNum);
      }

      continue;
    }
  }

  closeTurn(lines.length, null);

  // Resolve tool errors
  for (const tu of toolUses) {
    const tcId = tu.tool_call_id;
    if (tcId && tcId in toolResultIsError) {
      tu.is_error = toolResultIsError[tcId];
    }
  }

  // Per-record fallback only, for a record with no timestamp of its own.
  let firstEventTs = null;
  for (const e of events) {
    if (e.ts != null) { firstEventTs = e.ts; break; }
  }
  if (firstEventTs == null) {
    for (const m of metaEvents) {
      if (m.ts != null) { firstEventTs = m.ts; break; }
    }
  }

  // Build records from status_updates
  const records = [];
  let prevInput = 0;
  const ctxTurns = [];
  let turnIdx = 0;

  for (const m of metaEvents) {
    if (m.type !== "status_update") continue;
    const tu = m.token_usage || {};
    const fresh = tu.input_other || 0;
    const create = tu.input_cache_creation || 0;
    const read = tu.input_cache_read || 0;
    const output = tu.output || 0;
    const totalInput = fresh + create + read;

    // The wire names the model on every usage.record; honour it, canonicalised.
    const model = modelFor(m.wire_model, m.ts != null ? m.ts : firstEventTs);
    const rates = window.rateForModel(model);
    const cost = (
      fresh * rates.fresh / 1e6 +
      create * rates.create / 1e6 +
      read * rates.read / 1e6 +
      output * rates.output / 1e6
    );

    records.push({
      line_num: m.line,
      uuid: null,
      ts: m.ts,
      model,
      fresh_tokens: fresh,
      cache_creation_tokens: create,
      cache_read_tokens: read,
      output_tokens: output,
      cost_usd: cost,
      text_chars: textCharsSinceTurn,
      reply_latency_s: m.reply_latency_s != null ? m.reply_latency_s : null,
      ctx_input: totalInput,
    });

    if (totalInput > 0) {
      turnIdx++;
      ctxTurns.push({
        idx: turnIdx,
        ts: m.ts ? new Date(m.ts * 1000).toISOString() : "",
        line: m.line,
        input: totalInput,
        output: output,
        delta: totalInput - prevInput,
      });
      prevInput = totalInput;
    }
  }

  return {
    records,
    ctx_turns: ctxTurns,
    turn_count: ctxTurns.length,
    events,
    meta_events: metaEvents,
    tool_uses: toolUses,
  };
}

window.parseTranscript = function parseTranscript(blob) {
  const fmt = _detectFormat(blob);
  if (fmt === "kimi-code") return _parseKimiCode(blob);
  return _parseLegacy(blob);
};

// Per-call context-window input, codexmeter shape: a status_update's
// token_usage. Claudit's version summed input_tokens + cache_creation +
// cache_read and took the peak across usage.iterations (sub-agent fan-out);
// codexmeter's wire format has no iterations concept anywhere, so there is
// no peak branch here. Defensive: a missing or non-numeric field
// contributes 0, never NaN.
window.usageCtxInput = function usageCtxInput(u) {
  if (!u || typeof u !== "object") return 0;
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : 0);
  return num(u.input_other) + num(u.input_cache_creation) + num(u.input_cache_read);
};

// The ONE selector that decides "is this meta record a usage record, and
// what are its numbers". This parser emits type "status_update" with
// token_usage.{input_other,input_cache_creation,input_cache_read,output}
// plus a wire_model; claudit consumers read type "assistant_usage" with
// usage.*_tokens, a shape this parser never emits. Every consumer —
// computeSessionStats below, the Inspector's computeTurnStats, and
// app.jsx's txToDashData / loadFiles — goes through this helper so the
// record shape is derived in exactly one place and cannot drift again.
// Returns null for anything that is not a usage record.
//
// Numbers are defensive (same rule as usageCtxInput): a missing or
// non-numeric bucket contributes 0, never NaN. `model` is resolved
// per-record through modelFor — wire_model first, then the date ladder,
// with `fallbackTs` (the session's first event ts) for records carrying no
// timestamp of their own. `tsMs` is the record's timestamp in epoch ms for
// dashboard consumers. `sessionId` is a passthrough: the parser never sets
// it (one file ≈ one session); app.jsx's multi-file merge tags it.
window.asUsageRecord = function asUsageRecord(m, fallbackTs) {
  if (!m || m.type !== "status_update" || !m.token_usage) return null;
  const num = (v) => (typeof v === "number" && isFinite(v) ? v : 0);
  const tu = m.token_usage;
  const dt = parseTimestamp(m.ts);
  return {
    line: m.line,
    ts: m.ts,
    tsMs: dt ? dt.getTime() : null,
    sessionId: m.sessionId,
    model: modelFor(m.wire_model, m.ts != null ? m.ts : fallbackTs),
    input: num(tu.input_other),
    create: num(tu.input_cache_creation),
    read: num(tu.input_cache_read),
    output: num(tu.output),
    ctx: window.usageCtxInput(tu),
  };
};

// SessionHeader's stats, as an OBJECT. Ported from claudit's
// computeSessionStats onto codexmeter's parsed shapes: claudit read
// meta type "assistant_usage" with usage.{input,cache_creation,cache_read,
// output}_tokens; codexmeter's parser emits type "status_update" with
// token_usage.{input_other,input_cache_creation,input_cache_read,output}
// plus a wire_model, and event timestamps in epoch seconds (or ISO
// strings for legacy), which parseTimestamp normalises. Usage records are
// selected and normalised through window.asUsageRecord.
window.computeSessionStats = function computeSessionStats(events, metaEvents) {
  events = events || [];
  metaEvents = metaEvents || [];
  const stats = {
    firstTs: null, lastTs: null,  // epoch ms; SessionHeader derives duration
    turns: 0,
    userMsgs: 0,
    toolCalls: 0, toolResults: 0, errorResults: 0,
    parallelBatches: 0, parallelCalls: 0,
    toolCounts: {},
    fresh: 0, create: 0, read: 0, output: 0,
    totalInput: 0, hitRate: 0, cost: 0,
  };

  // Per-record model fallback for a status_update with no timestamp of its
  // own — same rule as the record builders above.
  let firstEventTs = null;

  for (const e of events) {
    const dt = parseTimestamp(e.ts);
    if (dt) {
      const t = dt.getTime();
      if (stats.firstTs == null || t < stats.firstTs) stats.firstTs = t;
      if (stats.lastTs == null || t > stats.lastTs) stats.lastTs = t;
      if (firstEventTs == null) firstEventTs = e.ts;
    }
    if (e.type === "tool_call" || e.type === "agent_spawn") {
      stats.toolCalls++;
      const name = e.tool_name || "";
      stats.toolCounts[name] = (stats.toolCounts[name] || 0) + 1;
    } else if (e.type === "user_message") {
      stats.userMsgs++;
    } else if (e.type === "tool_result") {
      stats.toolResults++;
      if (e.is_error) stats.errorResults++;
    }
    if (e.batch_size > 1 && e.batch_index === 1) {
      stats.parallelBatches++;
      stats.parallelCalls += e.batch_size;
    }
  }

  for (const m of metaEvents) {
    const u = window.asUsageRecord(m, firstEventTs);
    if (!u) continue;
    stats.turns++;
    stats.fresh += u.input;
    stats.create += u.create;
    stats.read += u.read;
    stats.output += u.output;
    // Per-record rates (the model was resolved inside asUsageRecord),
    // exactly as the record builders: a session can span a
    // cutoff or switch model mid-flight, so one session-wide rate would
    // misprice part of it.
    const r = window.rateForModel(u.model);
    stats.cost += (
      u.input * r.fresh / 1e6 +
      u.create * r.create / 1e6 +
      u.read * r.read / 1e6 +
      u.output * r.output / 1e6
    );
  }

  stats.totalInput = stats.fresh + stats.create + stats.read;
  stats.hitRate = stats.totalInput ? (stats.read / stats.totalInput) * 100 : 0;
  return stats;
};
