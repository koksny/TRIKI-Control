"""Replay a TRIKI session log (JSONL) offline.

Two jobs:
  1) Summarise what actually happened in a recorded session: how every motion
     sample was classified, WHY gestures were dropped (outcome histogram), what
     was emitted, and the key down/up timeline.
  2) Re-run the recorded raw motion through a FRESH detector with different
     parameters, so detector/tuning changes can be evaluated against a real
     session without the hardware.

Usage:
  .venv-windows/Scripts/python.exe triki_replay.py <session.jsonl>
  .venv-windows/Scripts/python.exe triki_replay.py <session.jsonl> --repeat-seconds 0.2 --min-confidence 0.5 --active-tail-seconds 0.30
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from triki_protocol import MotionSample
from triki_live import LiveGestureDetector


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def summarise(records: list[dict]) -> None:
    start = next((r for r in records if r.get("type") == "session_start"), None)
    if start:
        print("=== SESSION ===")
        print(f"  app {start.get('app_version')} | profile {start.get('active_profile')} | hold_ms {start.get('hold_ms')} | output {start.get('output_enabled')}")
        print(f"  actions: {start.get('actions')}")
        print(f"  detector: {start.get('detector')}")

    gestures = [r for r in records if r.get("type") == "gesture"]
    outcomes = Counter(r.get("outcome") for r in gestures)
    emitted = [r for r in gestures if r.get("outcome") == "emitted"]
    emitted_labels = Counter(r.get("label") for r in emitted)
    classified_labels = Counter(r.get("label") for r in gestures if r.get("label"))

    print("\n=== AS-RECORDED (live decisions) ===")
    print(f"  samples logged: {len(gestures)}")
    print(f"  outcomes: {dict(outcomes)}")
    print(f"  classified labels (all): {dict(classified_labels)}")
    print(f"  EMITTED labels: {dict(emitted_labels)}")

    # Key timeline: pair down->up per key to show how long keys were actually held.
    keys = [r for r in records if r.get("type") == "key"]
    if keys:
        print("\n=== KEY OUTPUT TIMELINE ===")
        holds: dict[str, float] = {}
        durations: dict[str, list[float]] = {}
        taps: Counter = Counter()
        for r in keys:
            action, key, t = r.get("action"), r.get("key"), r.get("t", 0.0)
            if action == "tap":
                taps[key] += 1
            elif action == "down":
                holds[key] = t
            elif action == "up" and key in holds:
                durations.setdefault(key, []).append(round(t - holds.pop(key), 3))
        if taps:
            print(f"  taps: {dict(taps)}")
        for key, ds in durations.items():
            avg = round(sum(ds) / len(ds), 3)
            print(f"  {key}: {len(ds)} hold(s), avg {avg}s, all={ds}")
        if not durations and not taps:
            print("  (no key output recorded — output was off or nothing emitted)")
    else:
        print("\n=== KEY OUTPUT TIMELINE ===\n  (no key events)")


def replay(records: list[dict], params: dict) -> None:
    samples = [(r["t"], r["values"]) for r in records if r.get("type") == "gesture" and "values" in r]
    if not samples:
        print("\n(no raw motion samples to replay)")
        return
    emitted: list[tuple] = []
    detector = LiveGestureDetector(
        observer=lambda rec: emitted.append((rec["t"], rec["label"]))
        if rec.get("outcome") == "emitted"
        else None,
        **params,
    )
    out = Counter()
    for index, (t, values) in enumerate(samples):
        result = detector.add_sample(t, MotionSample(packet_id=index, values=tuple(values)))
        out[result.label if result else "-"] += 1
    fired = Counter(label for _t, label in emitted)
    print("\n=== REPLAY WITH PARAMS ===")
    print(f"  params: {params}")
    print(f"  EMITTED labels: {dict(fired)} (total {len(emitted)})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Replay a TRIKI session log.")
    p.add_argument("path", type=Path)
    p.add_argument("--window-seconds", type=float, default=0.4)
    p.add_argument("--min-samples", type=int, default=6)
    p.add_argument("--min-confidence", type=float, default=0.55)
    p.add_argument("--repeat-seconds", type=float, default=0.3)
    p.add_argument("--warmup-seconds", type=float, default=0.05)
    p.add_argument("--active-tail-seconds", type=float, default=0.16)
    p.add_argument("--confirm-windows", type=int, default=1)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    records = load_records(args.path)
    if not records:
        print(f"No records in {args.path}")
        return 1
    summarise(records)
    replay(
        records,
        {
            "window_seconds": args.window_seconds,
            "min_samples": args.min_samples,
            "min_confidence": args.min_confidence,
            "repeat_seconds": args.repeat_seconds,
            "warmup_seconds": args.warmup_seconds,
            "active_tail_seconds": args.active_tail_seconds,
            "confirm_windows": args.confirm_windows,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
