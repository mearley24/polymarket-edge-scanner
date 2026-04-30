#!/usr/bin/env python3
"""
cross_venue_arb_scan.py — observer-only Polymarket-vs-Kalshi mirror scan.

Goal:
  Surface single-game binary sports markets that are listed on both
  Polymarket and Kalshi, and report the after-fee divergence between
  the two venues' executable prices. Prints a ranked list of
  opportunities. Does NOT place orders — execution remains a
  manual, human-approved step on each venue.

What this script does (and what it doesn't):
    * Hits the public Kalshi v2 API (no auth required for read).
    * Pulls Polymarket via Gamma + CLOB orderbook (executable best ASK,
      not the lagging last-trade price).
    * Aligns Polymarket's outcome labels to Kalshi's YES side via
      ``rules_primary`` parsing — Kalshi's ``yes_sub_title`` and
      ``no_sub_title`` are typically the same string and cannot be
      used to disambiguate sides.
    * Gates on settlement-time proximity (≤36 h) so a futures market
      ("Will the Canadiens win the Eastern Conference?") cannot match
      a single-game market ("Game 6: Tampa Bay at Montreal Winner?").
    * De-vig each side and compute cross-venue gap after taker fees.
    * Sort by net edge after fees, print ranked list.

Fees (assumed):
    Polymarket taker:         2.0%   (2026 schedule)
    Kalshi binary taker:      ~1.0%  (varies by series; we use the
                                       conservative 1% — sportsbook
                                       overhead floor)

How an arb actually executes (manual, NOT in this script):
    For a matched pair "Team A vs Team B" with Polymarket tokens
    (poly_A_yes, poly_B_yes) and Kalshi market kalshi_A_yes (with
    yes_ask + no_ask available):
        cheap_A_cost = min(poly_A_ask, kalshi_yes_ask)
        cheap_B_cost = min(poly_B_ask, kalshi_no_ask)
        if (cheap_A_cost + cheap_B_cost) * (1 + max_fee) < 1:
            arb exists, size = min(orderbook depth on each leg)

This script reports the candidates; the human picks one, places the
two orders manually (one per venue), and verifies they both filled
before considering the position locked.

Usage:
    python3 cross_venue_arb_scan.py
    python3 cross_venue_arb_scan.py --series KXNHLGAME,KXNBAGAME
    python3 cross_venue_arb_scan.py --min-edge-pct 1.0
    python3 cross_venue_arb_scan.py --output reports/$(date -u +%Y%m%dT%H%M%SZ)-cross-venue.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

POLY_FEE = 0.02
KALSHI_FEE = 0.01

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_BOOK = "https://clob.polymarket.com/book"
KALSHI_MKTS = "https://api.elections.kalshi.com/trade-api/v2/markets"

DEFAULT_KALSHI_SERIES = ["KXNHLGAME", "KXNBAGAME", "KXMLBGAME"]


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


# ── Polymarket ────────────────────────────────────────────────────────

_SPORTS_REGEX = re.compile(
    r"\b(nba|mlb|nhl|nfl|wnba|game [0-9]|match [0-9]|"
    r"vs\.?\s|@\s|will .* win|stars|wild|knicks|hawks|celtics|76ers|"
    r"nuggets|timberwolves|lakers|rockets|spurs|thunder|warriors|"
    r"clippers|nets|wizards|suns|mavericks|jazz|grizzlies|pelicans|"
    r"kings|trail blazers|magic|hornets|raptors|pacers|bucks|cavaliers|"
    r"pistons|bulls|heat|nationals|mets|royals|athletics|giants|"
    r"phillies|cardinals|pirates|braves|yankees|red sox|dodgers|"
    r"padres|astros|orioles|rangers|tigers|cubs|brewers|reds|"
    r"guardians|rays|marlins|blue jays|mariners|twins|white sox|"
    r"angels|d-?backs|rockies|d?evil rays|panthers|oilers|maple leafs|"
    r"canadiens|senators|sabres|bruins|rangers|islanders|flyers|"
    r"penguins|capitals|hurricanes|red wings|lightning|blackhawks|"
    r"predators|jets|wild|avalanche|blues|coyotes|kings|sharks|ducks|"
    r"flames|canucks|kraken|golden knights)",
    re.IGNORECASE,
)
_POLY_FUTURES_REGEX = re.compile(
    r"\b(202\d (nba|nhl|mlb|nfl) (finals|champion|playoffs|world series)|"
    r"win the 202\d|series winner|championship\?|champion\?|"
    r"before [a-z]+ 202\d|"
    r"eastern conference|western conference|atlantic division|"
    r"central division|metropolitan division|pacific division|"
    r"win.*conference|win.*division|win.*the al|win.*the nl|"
    r"in 4 games|in 5 games|in 6 games|in 7 games|sweep|"
    # Political elections — Polymarket lists thousands of these and the
    # team-name regex erroneously catches state names (Minnesota, Arizona).
    r"governor (race|election)|senate race|senate election|"
    r"presidential (election|race|nomination|primary)|"
    r"democratic primary|republican primary|"
    r"congressional|the (al|nl|fed)\b|"
    # Sub-markets that aren't moneylines — Kalshi's KX*GAME series is
    # game-winner only, so over/under, totals, spreads, and player props
    # are mismatches by definition.
    r"\b(o/u|over/under|over [0-9]|under [0-9]|spread|total goals?|"
    r"total runs?|total points?|first (half|period|quarter|inning)|"
    r"\d+\+ points?|\d+\+ assists?|\d+\+ rebounds?|\d+\+ goals?|"
    r"\d+\+ runs?|\d+\+ hits?|\d+\+ strikeouts?|\d+\+ pts|"
    r"leading after [0-9]|tied after [0-9]|to score first|"
    r"halftime|1st half|2nd half|game total|player to)|"
    # Esports — Kalshi's KX*GAME series is real-sport only, so any
    # Polymarket esports market can't cross-venue match. Exclude before
    # the matcher gets a chance to mistake "OpTic Texas" for the Texas
    # Rangers via the substring "Texas".
    r"\b(call of duty|cod\b|lol\b|league of legends|counter-?strike|"
    r"dota|valorant|rocket league|overwatch|esports?|"
    r"optic|fnatic|cloud9|t1\b|geng|ig\b|edg\b|jdg\b|"
    r"opening kickoff|tipoff|opening tipoff|opening period))",
    re.IGNORECASE,
)


def _parse_iso(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _settlement_proximity_ok(poly_end: str, kalshi_occurrence: str, max_hours: int = 36) -> bool:
    """Same-event matches must settle within max_hours of each other.

    A futures market ("win the Eastern Conference") will end weeks/months
    after the single Kalshi game; a real mirror ("Game 6 winner") settles
    within hours. Reject pairs where the gap exceeds the threshold.
    """
    pe = _parse_iso(poly_end)
    ke = _parse_iso(kalshi_occurrence)
    if pe is None or ke is None:
        return False  # be strict — no date = no match
    return abs((pe - ke).total_seconds()) <= max_hours * 3600


@dataclass
class PolyMarket:
    cid: str
    question: str
    outcomes: list[str]
    token_ids: list[str]
    asks: list[float] = field(default_factory=list)
    bids: list[float] = field(default_factory=list)
    end: str = ""

    @property
    def sum_asks(self) -> float:
        return sum(self.asks) if self.asks else 0.0


def fetch_polymarket_binary_sports() -> list[PolyMarket]:
    out: list[PolyMarket] = []
    seen: set[str] = set()
    # Order by volume24hr so the high-liquidity sports games come first.
    # Default ordering buries them under thousands of low-volume political
    # election sub-markets.
    for offset in (0, 500, 1000):
        page = curl_json(
            f"{GAMMA_URL}?active=true&closed=false&limit=500&offset={offset}"
            f"&order=volume24hr&ascending=false",
            timeout=15,
        )
        if not isinstance(page, list) or not page:
            break
        for m in page:
            cid = m.get("conditionId") or m.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            q = m.get("question") or ""
            if not q or _POLY_FUTURES_REGEX.search(q) or not _SPORTS_REGEX.search(q):
                continue
            ids_raw = m.get("clobTokenIds")
            if not ids_raw:
                continue
            try:
                ids = json.loads(ids_raw) if isinstance(ids_raw, str) else ids_raw
            except (TypeError, ValueError):
                continue
            if not isinstance(ids, list) or len(ids) != 2:
                continue
            try:
                last_px = float(m.get("lastTradePrice") or 0)
            except (TypeError, ValueError):
                last_px = 0.0
            if not 0.05 <= last_px <= 0.95:
                continue
            try:
                outs = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
            except (TypeError, ValueError):
                outs = []
            out.append(PolyMarket(
                cid=cid, question=q, outcomes=outs, token_ids=ids,
                end=m.get("endDate") or "",
            ))
        if len(page) < 500:
            break
    return out


def hydrate_poly_orderbook(pm: PolyMarket) -> bool:
    asks: list[float] = []
    bids: list[float] = []
    for tid in pm.token_ids:
        b = curl_json(f"{CLOB_BOOK}?token_id={tid}", timeout=5)
        if not b or not b.get("asks") or not b.get("bids"):
            return False
        try:
            asks.append(min(float(a["price"]) for a in b["asks"]))
            bids.append(max(float(x["price"]) for x in b["bids"]))
        except (KeyError, ValueError):
            return False
    pm.asks = asks
    pm.bids = bids
    return True


# ── Kalshi ────────────────────────────────────────────────────────────

@dataclass
class KalshiMarket:
    ticker: str
    title: str
    yes_ask: float
    yes_bid: float
    no_ask: float
    no_bid: float
    yes_subtitle: str
    no_subtitle: str
    occurrence: str
    rules_primary: str = ""

    def yes_team_text(self) -> str:
        """The team / outcome name attached to the YES side.

        Kalshi's ``yes_sub_title`` and ``no_sub_title`` are often both
        the same string (e.g. "MIN Wild" on a Game 6 winner market),
        because they describe what the market is *about*, not which
        outcome each side covers. The actual YES team appears in the
        ``rules_primary`` text: "If MIN Wild wins the Game 6: ...".
        We extract from there to disambiguate.
        """
        m = re.match(r"\s*if\s+([\w\s\.\-]+?)\s+win", self.rules_primary or "", re.IGNORECASE)
        if m:
            return m.group(1).strip()
        return self.yes_subtitle


def fetch_kalshi_series(series_ticker: str) -> list[KalshiMarket]:
    """One page of open markets in a series. Kalshi caps at ~1000/page."""
    d = curl_json(
        f"{KALSHI_MKTS}?limit=200&status=open&series_ticker={series_ticker}",
        timeout=10,
    )
    if not isinstance(d, dict):
        return []
    out: list[KalshiMarket] = []
    for m in d.get("markets", []):
        try:
            yes_ask = float(m.get("yes_ask_dollars") or 0)
            yes_bid = float(m.get("yes_bid_dollars") or 0)
            no_ask = float(m.get("no_ask_dollars") or 0)
            no_bid = float(m.get("no_bid_dollars") or 0)
        except (TypeError, ValueError):
            continue
        if yes_ask <= 0 or no_ask <= 0:
            continue
        out.append(KalshiMarket(
            ticker=m.get("ticker", ""),
            title=m.get("title", ""),
            yes_ask=yes_ask, yes_bid=yes_bid,
            no_ask=no_ask, no_bid=no_bid,
            yes_subtitle=m.get("yes_sub_title", ""),
            no_subtitle=m.get("no_sub_title", ""),
            occurrence=m.get("occurrence_datetime") or m.get("expected_expiration_time") or "",
            rules_primary=m.get("rules_primary", ""),
        ))
    return out


# ── Matching + arb math ───────────────────────────────────────────────

def normalize(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Drop common boilerplate
    for w in ("game", "winner", "the", "vs", "at", "will", "win"):
        s = re.sub(rf"\b{w}\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def fuzzy_match(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


@dataclass
class Opportunity:
    poly: PolyMarket
    kalshi: KalshiMarket
    similarity: float
    poly_side: str           # outcome label of side we'd buy on Polymarket
    kalshi_side: str         # "yes" or "no"
    poly_ask: float          # cost on Polymarket for cheap side
    kalshi_ask: float        # cost on Kalshi for opposite side
    sum_after_fees: float    # total $ outlay per $1 guaranteed payout
    net_edge_pct: float      # profit % after fees


def _align_poly_to_kalshi(poly: PolyMarket, kalshi: KalshiMarket) -> int | None:
    """Determine which Polymarket outcome corresponds to Kalshi YES.

    Returns 0 if poly.outcomes[0] semantically equals Kalshi's YES side,
    1 if poly.outcomes[1] does, or None if we can't tell. Matching uses
    fuzzy comparison against Kalshi's yes_sub_title / no_sub_title which
    typically carry the team abbreviation + name (e.g. "MIN Wild" vs
    Polymarket's outcome "Wild").
    """
    if not poly.outcomes or len(poly.outcomes) != 2:
        return None
    o0, o1 = poly.outcomes
    yes_team = kalshi.yes_team_text()
    if not yes_team:
        return None
    # Match each poly outcome against the Kalshi YES team. The higher
    # similarity wins. We use a meaningful gap (≥0.15) to reject
    # ambiguous matches where both poly outcomes look equally close.
    sim_o0 = fuzzy_match(o0, yes_team)
    sim_o1 = fuzzy_match(o1, yes_team)
    # Hard floor: the winning outcome must share at least 0.55 fuzzy
    # similarity with the Kalshi YES team. Without this gate, a market
    # like "Will SC Braga win" would "align" against "Kansas City vs A's"
    # because one of {SC Braga, the implicit other team} edges out the
    # other on alphabetic overlap, but neither is actually the same team.
    best = max(sim_o0, sim_o1)
    if best < 0.55:
        return None
    if abs(sim_o0 - sim_o1) < 0.15:
        return None
    return 0 if sim_o0 > sim_o1 else 1


def compute_cross_venue(poly: PolyMarket, kalshi: KalshiMarket) -> Opportunity | None:
    """Determine the cheapest valid arb pairing across the two venues.

    A valid arb pair consists of two legs that pay on OPPOSITE outcomes:
        - poly outcome X (YES) + kalshi side that covers the OTHER outcome.

    Crucially, this requires aligning Polymarket's outcome labels with
    Kalshi's yes_sub_title / no_sub_title. A naive pairing of
    (poly_a + kalshi_no) without alignment can produce two legs paying
    on the SAME outcome — which costs less than $1 but only pays $1 in
    one universe, not both, so the apparent "arb" is illusory and
    actually a doubled directional bet.
    """
    if not poly.asks or len(poly.asks) != 2:
        return None
    align = _align_poly_to_kalshi(poly, kalshi)
    if align is None:
        return None  # can't tell which poly outcome maps to Kalshi YES — skip

    # If poly[align] = kalshi YES side, the two valid arb pairs are:
    #   - (poly[align],   kalshi_NO )  ← both cover "kalshi YES outcome happens"? NO.
    # Wait: poly[align] pays on "kalshi YES outcome happens"; kalshi_NO pays
    # on "kalshi NO outcome happens" = the OPPOSITE outcome. Together they
    # cover both → valid arb.
    # The other valid pair is (poly[1-align], kalshi_YES), where poly[1-align]
    # pays on "kalshi NO outcome happens" and kalshi_YES pays on the opposite.
    yes_idx, no_idx = align, 1 - align
    pair_a_cost = poly.asks[yes_idx] + kalshi.no_ask   # poly YES side + kalshi NO
    pair_b_cost = poly.asks[no_idx] + kalshi.yes_ask   # poly NO side + kalshi YES

    if pair_a_cost <= pair_b_cost:
        poly_side = poly.outcomes[yes_idx]
        kalshi_side = "no"
        poly_ask_used = poly.asks[yes_idx]
        kalshi_ask_used = kalshi.no_ask
    else:
        poly_side = poly.outcomes[no_idx]
        kalshi_side = "yes"
        poly_ask_used = poly.asks[no_idx]
        kalshi_ask_used = kalshi.yes_ask

    total_with_fees = poly_ask_used * (1 + POLY_FEE) + kalshi_ask_used * (1 + KALSHI_FEE)
    net = 1.0 - total_with_fees
    return Opportunity(
        poly=poly, kalshi=kalshi,
        similarity=fuzzy_match(poly.question, kalshi.title),
        poly_side=poly_side, kalshi_side=kalshi_side,
        poly_ask=poly_ask_used, kalshi_ask=kalshi_ask_used,
        sum_after_fees=total_with_fees,
        net_edge_pct=net * 100,
    )


# ── Driver ────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--series", default=",".join(DEFAULT_KALSHI_SERIES),
                   help="comma-separated Kalshi series tickers")
    p.add_argument("--min-similarity", type=float, default=0.30,
                   help="lower bound on title fuzzy match — alignment + "
                        "proximity gates do most of the filtering, so this "
                        "can stay loose (default 0.30)")
    p.add_argument("--min-edge-pct", type=float, default=0.50,
                   help="report opportunities with after-fee edge >= this percent")
    p.add_argument("--output", default=None,
                   help="if set, also write a markdown report to this path")
    args = p.parse_args()

    series_list = [s.strip() for s in args.series.split(",") if s.strip()]
    print(f"Polymarket-vs-Kalshi cross-venue scan @ {dt.datetime.utcnow().isoformat(timespec='seconds')}Z")
    print(f"  series:           {series_list}")
    print(f"  min similarity:   {args.min_similarity}")
    print(f"  min edge pct:     {args.min_edge_pct}")
    print(f"  fees assumed:     poly={POLY_FEE*100:.1f}%  kalshi={KALSHI_FEE*100:.1f}%")
    print()

    # Pull both sides
    poly = fetch_polymarket_binary_sports()
    print(f"  Polymarket binary sports candidates: {len(poly)}")
    kalshi: list[KalshiMarket] = []
    for s in series_list:
        ks = fetch_kalshi_series(s)
        kalshi.extend(ks)
        print(f"  Kalshi {s}: {len(ks)}")
    if not kalshi:
        print("\nno Kalshi markets returned — public API may be empty for those series.")
        return 1
    if not poly:
        print("\nno Polymarket candidates — sports regex may need tightening.")
        return 1

    # Hydrate orderbook on Polymarket side (HTTP-bounded; do all candidates)
    print(f"\nhydrating Polymarket orderbooks for {len(poly)} markets...")
    hydrated = []
    for pm in poly:
        if hydrate_poly_orderbook(pm):
            hydrated.append(pm)
    print(f"  hydrated: {len(hydrated)} / {len(poly)}")

    # Match
    opportunities: list[Opportunity] = []
    for pm in hydrated:
        # Find best Kalshi match
        best: KalshiMarket | None = None
        best_sim = 0.0
        for km in kalshi:
            sim = fuzzy_match(pm.question, km.title + " " + km.yes_subtitle + " " + km.no_subtitle)
            if sim > best_sim:
                best_sim = sim
                best = km
        if best is None or best_sim < args.min_similarity:
            continue
        if not _settlement_proximity_ok(pm.end, best.occurrence):
            continue
        opp = compute_cross_venue(pm, best)
        if opp is None:
            continue
        opp.similarity = best_sim
        opportunities.append(opp)

    # Sort and print
    opportunities.sort(key=lambda o: -o.net_edge_pct)
    actionable = [o for o in opportunities if o.net_edge_pct >= args.min_edge_pct]

    print()
    print(f"matched pairs above similarity {args.min_similarity}: {len(opportunities)}")
    print(f"opportunities w/ net edge >= {args.min_edge_pct}%: {len(actionable)}")
    print()

    if not opportunities:
        print("(no matches)")
        return 0

    # Always print the top 10 even if below the edge bar — useful for sanity
    head = "  | sim  | poly_side          | kalshi_side | poly_ask | kal_ask | sum+fees | net%   | poly question / kalshi title"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for o in opportunities[:10]:
        flag = "*" if o.net_edge_pct >= args.min_edge_pct else " "
        line = (
            f"{flag} | {o.similarity:.2f} | "
            f"{o.poly_side[:18]:<18s} | "
            f"{o.kalshi_side:<11s} | "
            f"{o.poly_ask:>8.4f} | "
            f"{o.kalshi_ask:>7.4f} | "
            f"{o.sum_after_fees:>8.4f} | "
            f"{o.net_edge_pct:>+5.2f}% | "
            f"{o.poly.question[:55]} | {o.kalshi.title[:45]}"
        )
        print(line)

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Cross-venue (Polymarket × Kalshi) arb scan",
            f"_generated: {dt.datetime.utcnow().isoformat(timespec='seconds')}Z_",
            "",
            f"- series scanned: `{', '.join(series_list)}`",
            f"- min similarity: {args.min_similarity}",
            f"- min edge pct:   {args.min_edge_pct}",
            f"- assumed fees:   poly={POLY_FEE*100:.1f}%  kalshi={KALSHI_FEE*100:.1f}%",
            "",
            f"## Results — {len(actionable)} actionable / {len(opportunities)} matched",
            "",
            "| sim | poly side | kalshi side | poly ask | kalshi ask | total + fees | net edge % | poly question | kalshi title |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for o in opportunities[:30]:
            lines.append(
                f"| {o.similarity:.2f} | {o.poly_side} | {o.kalshi_side} | "
                f"${o.poly_ask:.4f} | ${o.kalshi_ask:.4f} | ${o.sum_after_fees:.4f} | "
                f"{o.net_edge_pct:+.2f}% | {o.poly.question[:80]} | {o.kalshi.title[:60]} |"
            )
        if not actionable:
            lines += [
                "",
                "## Verdict",
                "No actionable cross-venue arb at the requested minimum edge.",
            ]
        else:
            lines += [
                "",
                "## Verdict",
                f"{len(actionable)} matched pair(s) with after-fee edge ≥ {args.min_edge_pct}%.",
                "Manual approval required before executing any leg. Buy the cheap-side leg on each venue simultaneously and verify both fills before considering the position locked.",
            ]
        path.write_text("\n".join(lines) + "\n")
        print(f"\nwrote: {path}")

    return 0 if not actionable else 0


if __name__ == "__main__":
    sys.exit(main())
