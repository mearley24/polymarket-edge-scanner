#!/usr/bin/env python3
"""
scripts/edge_hunt.py — same-venue Polymarket "guaranteed winner" scan.

Three real categories of mathematical edge on a single venue:

  1. BINARY_COMPLEMENT  — on a 2-outcome market, YES_ask + NO_ask < $1
     after fees. Buy both sides, exactly one pays $1, lock in the
     spread. Rare on liquid markets, more common on long-tail.

  2. MULTI_OUTCOME_FIELD — on an N-outcome NegRisk market, the sum of
     YES asks across ALL candidates < $1 after fees. Exactly one
     outcome pays $1, so buying YES on every candidate locks in
     (1 − sum_asks − fees) per share regardless of which one wins.
     This is the highest-EV "guaranteed" play on Polymarket because
     market makers rarely keep every long-tail candidate quoted
     tight, and a single mispriced contender pulls the whole sum
     under $1.

  3. NEG_RISK_NO_FIELD   — on the same N-outcome market, you can also
     buy NO on every candidate. Exactly N−1 outcomes resolve NO, so
     buying NO on all N pays $(N−1). If sum_NO_asks < (N−1) − fees,
     it's an arb. This is symmetric to (2) but uses the NO side.

This script does NOT execute. Output is a ranked list of candidate
opportunities. The "guaranteed" label only applies if both legs
actually fill at quoted ask — orderbooks are thin on long-tail and
the second leg can vanish before the first prints. Verify orderbook
depth before sizing.

Usage:
    python3 edge_hunt.py
    python3 edge_hunt.py --min-edge-pct 1.0
    python3 edge_hunt.py --min-depth-shares 50
    python3 edge_hunt.py --output reports/$(date -u +%Y%m%dT%H%M%SZ)-edge-hunt.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

POLY_FEE = 0.02
GAMMA_URL = "https://gamma-api.polymarket.com/markets"
GAMMA_EVENTS = "https://gamma-api.polymarket.com/events"
CLOB_BOOK = "https://clob.polymarket.com/book"


def curl_json(url: str, timeout: int = 8) -> dict | list | None:
    r = subprocess.run(
        ["curl", "-s", "-m", str(timeout), url],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def fetch_book(token_id: str) -> tuple[float, float, float] | None:
    """Return (best_ask, best_ask_size, best_bid) or None on failure."""
    b = curl_json(f"{CLOB_BOOK}?token_id={token_id}", timeout=5)
    if not b or not b.get("asks") or not b.get("bids"):
        return None
    try:
        best_ask = min(float(a["price"]) for a in b["asks"])
        ask_size = sum(float(a["size"]) for a in b["asks"] if float(a["price"]) == best_ask)
        best_bid = max(float(x["price"]) for x in b["bids"])
        return best_ask, ask_size, best_bid
    except (KeyError, ValueError):
        return None


# ── Pull active markets ──────────────────────────────────────────────

def fetch_active_markets() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for offset in (0, 500, 1000, 1500, 2000):
        page = curl_json(
            f"{GAMMA_URL}?active=true&closed=false&limit=500&offset={offset}",
            timeout=15,
        )
        if not isinstance(page, list) or not page:
            break
        for m in page:
            cid = m.get("conditionId") or m.get("id")
            if cid and cid not in seen:
                seen.add(cid)
                out.append(m)
        if len(page) < 500:
            break
    return out


# ── Group markets into events (for multi-outcome scan) ───────────────

def group_into_events(markets: list[dict]) -> dict[str, list[dict]]:
    """Group markets by their parent event slug. Multi-outcome NegRisk
    events on Polymarket are exposed as one "event" with multiple
    "markets" (one per candidate), each with a binary YES/NO token
    pair where the YES side represents that candidate winning."""
    events: dict[str, list[dict]] = {}
    for m in markets:
        # Markets carry an "events" array with the parent event metadata.
        parent_slug = ""
        evs = m.get("events") or []
        if isinstance(evs, list) and evs:
            parent_slug = evs[0].get("slug") or evs[0].get("ticker") or ""
        if not parent_slug:
            parent_slug = m.get("slug") or m.get("conditionId", "")
        events.setdefault(parent_slug, []).append(m)
    return events


# ── Edge scans ───────────────────────────────────────────────────────

@dataclass
class BinaryComplementOpp:
    question: str
    cid: str
    end: str
    yes_outcome: str
    no_outcome: str
    yes_ask: float
    no_ask: float
    yes_depth: float
    no_depth: float
    sum_asks: float
    cost_after_fees: float
    edge_pct: float


@dataclass
class FieldArbOpp:
    event_slug: str
    n_outcomes: int
    sum_yes_asks: float
    cost_after_fees: float
    edge_pct: float
    min_depth: float
    candidates: list[tuple[str, float, float]]  # (label, yes_ask, ask_depth)


def scan_binary_complement(markets: list[dict], min_depth: float, executor: ThreadPoolExecutor) -> list[BinaryComplementOpp]:
    """Scan every binary market for YES_ask + NO_ask < $1 after fees."""
    opps: list[BinaryComplementOpp] = []
    todo: list[tuple[dict, list[str]]] = []
    for m in markets:
        if not m.get("clobTokenIds"):
            continue
        try:
            ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
        except (TypeError, ValueError):
            continue
        if not isinstance(ids, list) or len(ids) != 2:
            continue
        # Skip degenerate markets quickly
        try:
            last_px = float(m.get("lastTradePrice") or 0)
        except (TypeError, ValueError):
            last_px = 0.0
        # We DO want long-tail markets here, but completely-settled ones
        # (last_px ∈ {0, 1}) waste API calls.
        if last_px == 0.0 or last_px == 1.0:
            continue
        todo.append((m, ids))

    print(f"  binary candidates to hydrate: {len(todo)}", file=sys.stderr)

    futures = {executor.submit(fetch_book, tid): (m, tid, idx) for m, ids in todo for idx, tid in enumerate(ids)}
    book_by_tid: dict[str, tuple[float, float, float] | None] = {}
    for fut in as_completed(futures):
        _, tid, _ = futures[fut]
        try:
            book_by_tid[tid] = fut.result()
        except Exception:
            book_by_tid[tid] = None

    for m, ids in todo:
        b0 = book_by_tid.get(ids[0])
        b1 = book_by_tid.get(ids[1])
        if not b0 or not b1:
            continue
        ask0, depth0, _ = b0
        ask1, depth1, _ = b1
        if min(depth0, depth1) < min_depth:
            continue
        sum_asks = ask0 + ask1
        cost = ask0 * (1 + POLY_FEE) + ask1 * (1 + POLY_FEE)
        edge = (1.0 - cost) * 100
        if edge <= 0:
            continue
        outcomes = []
        try:
            outcomes = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
        except (TypeError, ValueError):
            outcomes = ["YES", "NO"]
        opps.append(BinaryComplementOpp(
            question=m.get("question", ""),
            cid=m.get("conditionId") or m.get("id", ""),
            end=m.get("endDate", ""),
            yes_outcome=outcomes[0] if outcomes else "YES",
            no_outcome=outcomes[1] if len(outcomes) >= 2 else "NO",
            yes_ask=ask0, no_ask=ask1,
            yes_depth=depth0, no_depth=depth1,
            sum_asks=sum_asks,
            cost_after_fees=cost,
            edge_pct=edge,
        ))
    return opps


def scan_field_arb(events: dict[str, list[dict]], min_outcomes: int, min_depth: float, executor: ThreadPoolExecutor) -> list[FieldArbOpp]:
    """For each event with ≥min_outcomes binary sub-markets, sum the
    YES asks across every candidate; flag if < $1 after fees.

    Caveat: this only works when the event is genuinely
    mutually-exclusive (NegRisk). Some Polymarket "events" are loose
    groupings of unrelated binaries (e.g., "Will any of these things
    happen by Q4?") where multiple can resolve YES — the sum-of-YES
    test fails for those because more than one can pay. We filter on
    ``negRisk == True`` if available; otherwise we keep the result
    but flag it as ``unverified``.
    """
    opps: list[FieldArbOpp] = []
    candidate_events: list[tuple[str, list[dict], bool]] = []
    for slug, ms in events.items():
        if len(ms) < min_outcomes:
            continue
        # All children must be binary
        if any(
            not (
                m.get("clobTokenIds")
                and len(json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]) == 2
            )
            for m in ms
        ):
            continue
        # NegRisk flag: market is mutually exclusive (exactly one resolves YES).
        # NegRiskAugmented flag: there's an IMPLICIT "Other" outcome that
        # isn't a tradable market. If augmented=True, sum_yes < 1 just
        # reflects the probability that a listed outcome wins; buying YES
        # on every listed candidate has negative EV when augmented.
        parent = (ms[0].get("events") or [{}])[0]
        is_negrisk = bool(ms[0].get("negRisk") or parent.get("negRisk"))
        is_augmented = bool(ms[0].get("negRiskAugmented") or parent.get("negRiskAugmented"))
        if is_augmented:
            # Skip — sum_yes is by construction less than 1 because of
            # the implicit Other slot; not a real arb.
            continue
        candidate_events.append((slug, ms, is_negrisk))

    print(f"  candidate events for field-arb: {len(candidate_events)}", file=sys.stderr)

    # Hydrate YES side (token_id[0]) of every candidate market
    tids: list[str] = []
    tid_to_market: dict[str, dict] = {}
    for slug, ms, _ in candidate_events:
        for m in ms:
            try:
                ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
            except (TypeError, ValueError):
                continue
            if not isinstance(ids, list) or len(ids) != 2:
                continue
            tids.append(ids[0])  # YES token
            tid_to_market[ids[0]] = m

    futures = {executor.submit(fetch_book, t): t for t in tids}
    book_by_tid: dict[str, tuple[float, float, float] | None] = {}
    for fut in as_completed(futures):
        t = futures[fut]
        try:
            book_by_tid[t] = fut.result()
        except Exception:
            book_by_tid[t] = None

    for slug, ms, is_negrisk in candidate_events:
        per_candidate: list[tuple[str, float, float]] = []
        ok = True
        for m in ms:
            try:
                ids = json.loads(m["clobTokenIds"]) if isinstance(m["clobTokenIds"], str) else m["clobTokenIds"]
            except (TypeError, ValueError):
                ok = False; break
            yes_tid = ids[0]
            book = book_by_tid.get(yes_tid)
            if not book:
                ok = False; break
            ask, depth, _ = book
            label = m.get("groupItemTitle") or m.get("question") or m.get("slug","")
            per_candidate.append((label[:60], ask, depth))
        if not ok or not per_candidate:
            continue
        sum_yes = sum(c[1] for c in per_candidate)
        # After fees: buying YES on each costs ask*(1+fee)
        cost_after_fees = sum(c[1] * (1 + POLY_FEE) for c in per_candidate)
        edge = (1.0 - cost_after_fees) * 100
        # Field-arb requires the event to be NegRisk (mutually exclusive)
        # AND total sum_yes < 1. If not negrisk, payout structure is
        # different — skip.
        if not is_negrisk:
            continue
        if edge <= 0:
            continue
        min_d = min(c[2] for c in per_candidate)
        if min_d < min_depth:
            continue
        opps.append(FieldArbOpp(
            event_slug=slug,
            n_outcomes=len(per_candidate),
            sum_yes_asks=sum_yes,
            cost_after_fees=cost_after_fees,
            edge_pct=edge,
            min_depth=min_d,
            candidates=per_candidate,
        ))
    return opps


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-edge-pct", type=float, default=0.30,
                   help="report opportunities with edge ≥ this percent (default 0.30)")
    p.add_argument("--min-depth-shares", type=float, default=5.0,
                   help="reject opportunities where any leg has best-ask depth < this many shares (default 5; multi-outcome long-tail markets often have very thin per-leg books)")
    p.add_argument("--min-outcomes", type=int, default=4,
                   help="multi-outcome scan: require ≥N candidate sub-markets (default 4)")
    p.add_argument("--output", default=None,
                   help="if set, write a markdown report to this path")
    p.add_argument("--workers", type=int, default=12,
                   help="concurrent CLOB orderbook fetches")
    args = p.parse_args()

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"Polymarket same-venue edge hunt @ {now}")
    print(f"  min edge:   {args.min_edge_pct}%")
    print(f"  min depth:  {args.min_depth_shares} shares")
    print(f"  poly fee:   {POLY_FEE * 100:.1f}%")
    print()

    markets = fetch_active_markets()
    print(f"  active markets: {len(markets)}")

    events = group_into_events(markets)
    print(f"  distinct events: {len(events)}")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        binary = scan_binary_complement(markets, args.min_depth_shares, ex)
        binary.sort(key=lambda o: -o.edge_pct)
        binary_actionable = [o for o in binary if o.edge_pct >= args.min_edge_pct]

        field = scan_field_arb(events, args.min_outcomes, args.min_depth_shares, ex)
        field.sort(key=lambda o: -o.edge_pct)
        field_actionable = [o for o in field if o.edge_pct >= args.min_edge_pct]

    print()
    print(f"=== Binary complement (YES + NO < $1 after {POLY_FEE*100:.0f}% fee) ===")
    print(f"  found: {len(binary)}   actionable @ ≥{args.min_edge_pct}%: {len(binary_actionable)}")
    if binary:
        print()
        print(f"  | edge%  | yes_ask | no_ask | min_depth | end           | question")
        print(f"  | -------|---------|--------|-----------|---------------|---------")
        for o in binary[:15]:
            flag = "*" if o.edge_pct >= args.min_edge_pct else " "
            print(f"  {flag} {o.edge_pct:>+5.2f}% | {o.yes_ask:.4f} | {o.no_ask:.4f} | {min(o.yes_depth, o.no_depth):>9.0f} | {o.end[:13] if o.end else '—':13s} | {o.question[:60]}")

    print()
    print(f"=== Multi-outcome NegRisk field arb (sum of YES < $1 after fees) ===")
    print(f"  found: {len(field)}   actionable @ ≥{args.min_edge_pct}%: {len(field_actionable)}")
    if field:
        print()
        print(f"  | edge%   | n  | sum_yes | min_depth | event")
        print(f"  | --------|----|---------|-----------|------")
        for o in field[:10]:
            flag = "*" if o.edge_pct >= args.min_edge_pct else " "
            print(f"  {flag} {o.edge_pct:>+6.2f}% | {o.n_outcomes:2d} | {o.sum_yes_asks:.4f} | {o.min_depth:>9.0f} | {o.event_slug[:60]}")
            if o.edge_pct >= args.min_edge_pct:
                # Show the cheapest 5 legs that drive the arb
                top = sorted(o.candidates, key=lambda c: c[1])[:5]
                for label, ask, depth in top:
                    print(f"          {label[:55]:<55s}  ask=${ask:.4f}  depth={depth:.0f}")

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Polymarket same-venue edge hunt",
            f"_generated: {now}_",
            "",
            f"Active markets scanned: {len(markets)} across {len(events)} events.",
            f"Min edge: **{args.min_edge_pct}%**, min orderbook depth: **{args.min_depth_shares} shares** per leg.",
            "",
            "## Binary complement opportunities (YES + NO < $1)",
            "",
            f"- found: {len(binary)}",
            f"- actionable at ≥ {args.min_edge_pct}%: {len(binary_actionable)}",
            "",
            "| edge % | yes ask | no ask | min depth | end date | question |",
            "|---|---|---|---|---|---|",
        ]
        for o in binary[:30]:
            lines.append(f"| {o.edge_pct:+.2f}% | ${o.yes_ask:.4f} | ${o.no_ask:.4f} | {min(o.yes_depth, o.no_depth):.0f} | {o.end[:13] if o.end else '—'} | {o.question[:80]} |")

        lines += [
            "",
            "## Multi-outcome NegRisk field arb",
            "",
            f"- found: {len(field)}",
            f"- actionable at ≥ {args.min_edge_pct}%: {len(field_actionable)}",
            "",
            "| edge % | n | sum yes | min depth | event |",
            "|---|---|---|---|---|",
        ]
        for o in field[:20]:
            lines.append(f"| {o.edge_pct:+.2f}% | {o.n_outcomes} | ${o.sum_yes_asks:.4f} | {o.min_depth:.0f} | {o.event_slug[:60]} |")
        path.write_text("\n".join(lines) + "\n")
        print(f"\nwrote: {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
