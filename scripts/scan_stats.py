#!/usr/bin/env python3
"""
scripts/scan_stats.py -- summary statistics from reports/scan_log.csv

Reads the structured CSV produced by edge_hunt.py --output and prints
a human-readable frequency report.  Once the CSV has >= 30 rows the
output is suitable for a public dataset summary.

Usage:
    python3 scripts/scan_stats.py
    python3 scripts/scan_stats.py --csv reports/scan_log.csv
    python3 scripts/scan_stats.py --json          # machine-readable
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        return list(reader)


def _pct(n: int, total: int) -> str:
    return f"{100 * n // total}%" if total else "n/a"


def main() -> int:
    parser = argparse.ArgumentParser(description="Polymarket edge-scan trend statistics")
    parser.add_argument("--csv",  default="reports/scan_log.csv", help="Path to scan_log.csv")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    args = parser.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"error: {path} not found. Run edge_hunt.py --output reports/... first.")
        return 1

    rows = load_rows(path)
    n = len(rows)
    if n == 0:
        print("No data yet.")
        return 0

    dates             = [r["date"] for r in rows]
    binary_counts     = [int(r["binary_count"]) for r in rows]
    binary_actionable = [int(r["binary_actionable"]) for r in rows]
    field_counts      = [int(r["field_count"]) for r in rows]
    field_actionable  = [int(r["field_actionable"]) for r in rows]
    field_edges       = [float(r["field_max_edge_pct"]) for r in rows if float(r["field_max_edge_pct"]) > 0]
    field_depths      = [float(r["field_max_depth"])    for r in rows if float(r["field_max_depth"])    > 0]

    binary_days  = sum(1 for c in binary_actionable if c > 0)
    field_days   = sum(1 for c in field_actionable  if c > 0)

    summary = {
        "scans":        n,
        "period_start": dates[0],
        "period_end":   dates[-1],
        "binary": {
            "days_with_any_opp":        sum(1 for c in binary_counts     if c > 0),
            "days_with_actionable_opp": binary_days,
            "actionable_rate":          f"{_pct(binary_days, n)}",
            "avg_per_day":              round(statistics.mean(binary_counts), 2),
            "peak_in_one_day":          max(binary_counts),
        },
        "field_arb": {
            "days_with_any_opp":        sum(1 for c in field_counts    if c > 0),
            "days_with_actionable_opp": field_days,
            "actionable_rate":          f"{_pct(field_days, n)}",
            "avg_per_day":              round(statistics.mean(field_counts), 2),
            "max_edge_pct":             round(max(field_edges),              4) if field_edges  else 0.0,
            "avg_edge_pct":             round(statistics.mean(field_edges),  4) if field_edges  else 0.0,
            "avg_best_depth_shares":    round(statistics.mean(field_depths), 1) if field_depths else 0.0,
        },
        "dataset_ready": n >= 30,
    }

    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    print(f"Polymarket Edge Scanner -- {n} daily scan{'s' if n != 1 else ''}")
    print(f"Period : {summary['period_start']} to {summary['period_end']}")
    print()
    b = summary["binary"]
    print("Binary complement opportunities (YES + NO < $1 after fees):")
    print(f"  Days with any opp        : {b['days_with_any_opp']}/{n}")
    print(f"  Days with actionable opp : {b['days_with_actionable_opp']}/{n}  ({b['actionable_rate']})")
    print(f"  Avg opps per day         : {b['avg_per_day']}")
    print(f"  Peak in one day          : {b['peak_in_one_day']}")
    print()
    f = summary["field_arb"]
    print("Multi-outcome NegRisk field arb:")
    print(f"  Days with any opp        : {f['days_with_any_opp']}/{n}")
    print(f"  Days with actionable opp : {f['days_with_actionable_opp']}/{n}  ({f['actionable_rate']})")
    if f["max_edge_pct"]:
        print(f"  Best edge seen           : +{f['max_edge_pct']:.2f}%")
        print(f"  Avg edge (when any)      : +{f['avg_edge_pct']:.2f}%")
    if f["avg_best_depth_shares"]:
        print(f"  Avg depth (best leg)     : {f['avg_best_depth_shares']:.0f} shares")
    print()
    if summary["dataset_ready"]:
        print(f"Dataset milestone reached ({n} >= 30 scans). Ready for public summary.")
    else:
        left = 30 - n
        print(f"{left} more scan{'s' if left != 1 else ''} until the 30-day dataset milestone.")

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())