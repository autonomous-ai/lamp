"""
Brain benchmark report — summarises BrainBenchmark JSONL into a quick
comparison table per provider.

Usage:
    python -m lelamp.brain_benchmark_report           # last 24h, default dir
    python -m lelamp.brain_benchmark_report --dir /root/local/brain_bench
    python -m lelamp.brain_benchmark_report --since 2026-05-22
    python -m lelamp.brain_benchmark_report --json    # raw stats

Reads every ``*.jsonl`` file in ``--dir`` (default
``/root/local/brain_bench``), groups by ``provider`` (gemini / openai
/ …) and prints:

  - turn count, decision breakdown (% chit-chat / % delegate / % error)
  - latency percentiles (first reply token, decision finalised)
  - cumulative token cost

Designed to run on the Pi alongside the lumi-lelamp service. Pure
stdlib — no extra deps.
"""

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

DEFAULT_DIR = os.environ.get("LELAMP_BRAIN_BENCH_DIR", "/root/local/brain_bench")


def _iter_records(paths: Iterable[Path], since: float) -> Iterable[dict]:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("started_at", 0) < since:
                        continue
                    yield obj
        except OSError as e:
            print(f"warning: cannot read {path}: {e}", file=sys.stderr)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    values = sorted(values)
    k = (len(values) - 1) * p / 100
    f, c = int(k), min(int(k) + 1, len(values) - 1)
    return values[f] + (values[c] - values[f]) * (k - f)


def _fmt_secs(v: float | None) -> str:
    if v is None or v != v:  # NaN
        return "  -  "
    if v < 10:
        return f"{v:.2f}s"
    return f"{v:.1f}s"


def _fmt_pct(num: int, denom: int) -> str:
    if denom <= 0:
        return "  - "
    return f"{100 * num / denom:5.1f}%"


def _summarise(records: list[dict]) -> dict:
    by_provider: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_provider[r.get("provider", "?")].append(r)

    out: dict[str, dict] = {}
    for provider, rs in by_provider.items():
        decisions = [r.get("decision", "?") for r in rs]
        n = len(rs)
        chit = sum(1 for d in decisions if d == "chitchat")
        deleg = sum(1 for d in decisions if d == "delegate")
        err = sum(1 for d in decisions if d == "error")
        empty = sum(1 for d in decisions if d == "empty")

        def lat(field: str, only: str | None = None) -> list[float]:
            return [
                r[field] for r in rs
                if r.get(field) is not None
                and (only is None or r.get("decision") == only)
            ]

        first_token_chit = lat("latency_first_token_s", only="chitchat")
        first_audio_chit = lat("latency_first_audio_s", only="chitchat")
        decision_deleg = lat("latency_decision_s", only="delegate")
        decision_chit = lat("latency_decision_s", only="chitchat")

        prompt = sum(r.get("prompt_tokens", 0) for r in rs)
        response = sum(r.get("response_tokens", 0) for r in rs)
        total = sum(r.get("total_tokens", 0) for r in rs)

        out[provider] = {
            "count": n,
            "chitchat": chit, "delegate": deleg, "error": err, "empty": empty,
            "latency_first_token_chit_p50": _percentile(first_token_chit, 50),
            "latency_first_token_chit_p95": _percentile(first_token_chit, 95),
            "latency_first_audio_chit_p50": _percentile(first_audio_chit, 50),
            "latency_first_audio_chit_p95": _percentile(first_audio_chit, 95),
            "latency_decision_deleg_p50": _percentile(decision_deleg, 50),
            "latency_decision_deleg_p95": _percentile(decision_deleg, 95),
            "latency_decision_chit_p50": _percentile(decision_chit, 50),
            "latency_decision_chit_p95": _percentile(decision_chit, 95),
            "tokens_prompt": prompt,
            "tokens_response": response,
            "tokens_total": total,
        }
    return out


def _print_table(stats: dict[str, dict]) -> None:
    if not stats:
        print("(no records in range)")
        return
    providers = sorted(stats)
    sep = "-" * 78
    print(sep)
    print(f"{'Metric':<38}" + "".join(f"{p:>13}" for p in providers))
    print(sep)

    def row(label: str, key: str, fmt=str):
        cols = "".join(f"{fmt(stats[p].get(key)):>13}" for p in providers)
        print(f"{label:<38}{cols}")

    def row_pct(label: str, num_key: str):
        cols = ""
        for p in providers:
            cols += f"{_fmt_pct(stats[p][num_key], stats[p]['count']):>13}"
        print(f"{label:<38}{cols}")

    row("turns", "count", str)
    row_pct("chit-chat %", "chitchat")
    row_pct("delegate %", "delegate")
    row_pct("error %", "error")
    row_pct("empty %", "empty")
    print(sep)
    print("Latency — chit-chat (sec from first mic frame to ...)")
    row("  first reply token (p50)", "latency_first_token_chit_p50", _fmt_secs)
    row("  first reply token (p95)", "latency_first_token_chit_p95", _fmt_secs)
    row("  first reply audio (p50)", "latency_first_audio_chit_p50", _fmt_secs)
    row("  first reply audio (p95)", "latency_first_audio_chit_p95", _fmt_secs)
    row("  speech complete    (p50)", "latency_decision_chit_p50", _fmt_secs)
    row("  speech complete    (p95)", "latency_decision_chit_p95", _fmt_secs)
    print("Latency — delegate (sec to dispatch)")
    row("  decision (p50)", "latency_decision_deleg_p50", _fmt_secs)
    row("  decision (p95)", "latency_decision_deleg_p95", _fmt_secs)
    print(sep)
    print("Tokens (cumulative)")
    row("  prompt", "tokens_prompt", lambda v: f"{v:,}" if v else "0")
    row("  response", "tokens_response", lambda v: f"{v:,}" if v else "0")
    row("  total", "tokens_total", lambda v: f"{v:,}" if v else "0")
    print(sep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=DEFAULT_DIR, help="bench JSONL directory")
    ap.add_argument("--since", default=None,
                    help="ISO date or '24h' / '7d' (default last 24h)")
    ap.add_argument("--json", action="store_true",
                    help="output raw stats as JSON instead of a table")
    args = ap.parse_args()

    bench_dir = Path(args.dir)
    if not bench_dir.is_dir():
        print(f"bench dir not found: {bench_dir}", file=sys.stderr)
        return 1

    if args.since is None:
        since = (datetime.now() - timedelta(hours=24)).timestamp()
    elif args.since.endswith("m") and args.since[:-1].isdigit():
        since = (datetime.now() - timedelta(minutes=int(args.since[:-1]))).timestamp()
    elif args.since.endswith("h") and args.since[:-1].isdigit():
        since = (datetime.now() - timedelta(hours=int(args.since[:-1]))).timestamp()
    elif args.since.endswith("d") and args.since[:-1].isdigit():
        since = (datetime.now() - timedelta(days=int(args.since[:-1]))).timestamp()
    else:
        since = datetime.fromisoformat(args.since).timestamp()

    files = sorted(bench_dir.glob("*.jsonl"))
    records = list(_iter_records(files, since))
    stats = _summarise(records)

    if args.json:
        print(json.dumps(stats, indent=2, default=str))
    else:
        since_iso = datetime.fromtimestamp(since).isoformat(timespec="minutes")
        print(f"Brain benchmark report — {len(records)} turns since {since_iso}")
        _print_table(stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
