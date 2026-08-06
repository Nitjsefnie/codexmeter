#!/usr/bin/env python3
"""Run backend.parse.parse_file over every local Codex rollout and report.

A development harness for the Codex format, not part of the service: it is
how a parse change is checked against real transcripts before it ships. It
prints the corpus totals the four documented traps are about, so a
regression shows up as a moved number rather than as an exception nobody
sees.

  python3 scripts/parse_codex_corpus.py [ROOT] [--verbose]

ROOT defaults to ~/.codex/sessions. Exit status is 1 if any file failed.
"""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# pylint: disable=wrong-import-position
# The sys.path line above is what makes `backend` importable when this runs
# as a bare script, so these imports cannot precede it.
import orjson  # noqa: E402

from backend import parse  # noqa: E402


TOKEN_KEYS = ("fresh_tokens", "cache_creation_tokens", "cache_read_tokens",
              "output_tokens")


def _naive_final_total(blob: bytes) -> int:
    """The wrong answer: this file's LAST cumulative total_tokens.

    Summing this per file is the mistake trap 1 is about — it charges every
    forked thread for its parent's history. Reported beside the real total
    so the gap stays visible instead of being an argument in a docstring.
    """
    last = 0
    for raw in blob.splitlines():
        if b'"token_count"' not in raw:
            continue
        try:
            obj = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        payload = obj.get("payload") or {}
        if payload.get("type") != "token_count":
            continue
        info = payload.get("info") or {}
        total = (info.get("total_token_usage") or {}).get("total_tokens")
        if total:
            last = int(total)
    return last


class _Totals:
    """Running corpus totals, accumulated one parsed file at a time."""

    def __init__(self):
        self.ok = 0
        self.failed = 0
        self.cost = 0.0
        self.naive_final = 0
        self.counts = {"records": 0, "tool_uses": 0, "turns": 0,
                       "rate_limits": 0, "added": 0, "deleted": 0}
        self.tokens = dict.fromkeys(TOKEN_KEYS, 0)
        self.models: dict[str, int] = {}

    def add(self, out: dict, blob: bytes) -> float:
        """Fold one parsed file in; returns that file's cost."""
        self.ok += 1
        file_cost = sum(r["cost_usd"] for r in out["records"])
        self.cost += file_cost
        self.counts["records"] += len(out["records"])
        self.counts["tool_uses"] += len(out["tool_uses"])
        self.counts["turns"] += out["turn_count"]
        self.counts["rate_limits"] += len(out["rate_limit_hits"])
        for rec in out["records"]:
            for key in TOKEN_KEYS:
                self.tokens[key] += rec[key]
            self.models[rec["model"]] = self.models.get(rec["model"], 0) + 1
        for tool_use in out["tool_uses"]:
            self.counts["added"] += tool_use["lines_added"]
            self.counts["deleted"] += tool_use["lines_deleted"]
        self.naive_final += _naive_final_total(blob)
        return file_cost

    @property
    def billed(self) -> int:
        return sum(self.tokens.values())

    def report(self) -> None:
        print()
        print(f"files parsed cleanly: {self.ok}   failed: {self.failed}")
        for label in ("records", "tool_uses", "turns", "rate_limits"):
            print(f"{label + ':':<22}{self.counts[label]:>14,}")
        for key in TOKEN_KEYS:
            print(f"{key + ':':<22}{self.tokens[key]:>14,}")
        print(f"{'lines added/deleted:':<22}{self.counts['added']:>14,} / "
              f"{self.counts['deleted']:,}")
        print(f"{'cost USD:':<22}{self.cost:>14.4f}")
        print(f"{'models:':<22}{self.models}")
        print()
        print("TRAP 1 — summing each file's FINAL cumulative counter instead:")
        print(f"  differenced billed tokens: {self.billed:>15,}")
        print(f"  naive per-file finals:     {self.naive_final:>15,}")
        if self.billed:
            print("  naive inflation:           "
                  f"{self.naive_final / self.billed:>15.2f}x")


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "--verbose" in argv[1:]
    root = Path(args[0]) if args else Path(
        os.path.expanduser("~/.codex/sessions"))
    files = sorted(root.glob("**/rollout-*.jsonl"))
    if not files:
        print(f"no rollout files under {root}")
        return 1

    totals = _Totals()
    if verbose:
        print(f"{'file':<52} {'recs':>6} {'tools':>6} {'turns':>6} {'cost':>10}")
    for path in files:
        blob = path.read_bytes()
        try:
            out = parse.parse_file(str(path), blob)
        except Exception:  # pylint: disable=broad-except
            totals.failed += 1
            print(f"{path.name[:52]:<52}  PARSE FAILED")
            traceback.print_exc()
            continue
        file_cost = totals.add(out, blob)
        if verbose:
            print(f"{path.name[:52]:<52} {len(out['records']):>6} "
                  f"{len(out['tool_uses']):>6} {out['turn_count']:>6} "
                  f"{file_cost:>10.4f}")

    totals.report()
    return 1 if totals.failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
