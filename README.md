# polymarket-edge-scanner

Two read-only scripts that scan [Polymarket](https://polymarket.com) for the
three real categories of mathematical edge that exist on a prediction market:

1. **Binary complement arb** — `YES_ask + NO_ask < $1` after fees on a
   2-outcome market. Buy both, exactly one pays $1, lock the spread.
2. **Multi-outcome NegRisk field arb** — sum of `YES_ask` across all
   candidates of a mutually-exclusive event `< $1` after fees. Exactly one
   outcome resolves YES, so buying YES on every candidate locks
   `(1 − sum_asks − fees)` per share regardless of which one wins.
3. **Cross-venue arb** — Polymarket vs [Kalshi](https://kalshi.com) on the
   same underlying event (e.g., tonight's NHL game). Two independent order
   books occasionally diverge by 2–5pp at game time.

Both scripts are **observer-only** — they output ranked candidate lists, they
don't place orders. Execution is your problem (and you should think hard
about whether you want to do it; see the caveats below).

## Why I wrote this — and the +46% "guaranteed arb" that wasn't

The first time I ran the multi-outcome scanner on Polymarket, it printed:

```
=== Multi-outcome NegRisk field arb (sum of YES < $1 after fees) ===
  *  +46.55% | 20 | sum_yes=$0.5240  | event: nobel-peace-prize-winner-2026
          Vladimir Putin           ask=$0.0050  depth=3221
          Benjamin Netanyahu       ask=$0.0050  depth=3349
          Julian Assange           ask=$0.0060  depth=4570
          Elon Musk                ask=$0.0070  depth=868
          António Guterres         ask=$0.0070  depth=4236
          ...
```

Twenty named candidates, sum of best YES asks across all of them = **$0.524**.
Buying YES on every candidate (after a 2% fee per leg) costs about $0.535
total. If exactly one outcome resolves YES, you collect $1 — that's a
**46.55% guaranteed return** in a few months.

Except it isn't.

Polymarket's `negRiskAugmented: True` flag — undocumented in most tutorials —
means there's an **implicit "Other" outcome** that doesn't appear as a
tradable market. The 20 named candidates don't form an exhaustive set. They
form a partial set, and the market is correctly pricing them at 52.4%
combined probability of one of them winning, with the remaining 47.6%
allocated to "anyone not on this list." If a non-listed person wins the
Nobel Peace Prize, **all 20 markets resolve NO** and you collect $0.

The expected value of buying YES on all 20:

```
EV = 0.524 × $1.00  −  $0.535 cost  =  −$0.011 per share = NEGATIVE
```

The "+46%" is the price the market is paying you to take the field bet that
one of these 20 specific people wins, and that bet is fairly priced. Not
arbitrage. Not even close.

The scanner now filters events with `negRiskAugmented == True` before
flagging field arbs. The same filter would have prevented every
James-Bond-actor / Eurovision-winner / Heisman-winner false positive that
catches first-time scanners. **If you're building one of these yourself,
this is the trap to know about first.**

## What the scanners actually find

Tonight, on a fresh run against ~2500 active Polymarket markets across 454
events:

```
=== Binary complement (YES + NO < $1 after 2% fee) ===
  found: 0   actionable @ ≥0.3%: 0

=== Multi-outcome NegRisk field arb (sum of YES < $1 after fees) ===
  found: 3   actionable @ ≥0.3%: 3

  | edge%   | n  | sum_yes | min_depth | event
  | --------|----|---------|-----------|------
  *  +9.53% |  8 | 0.8870  |     18    | how-many-gold-cards-will-trump-sell-in-2026
  *  +3.20% |  7 | 0.9490  |    297    | openai-ipo-closing-market-cap
  *  +1.77% |  6 | 0.9630  |      6    | harvey-weinstein-prison-time
```

All three are real (`negRiskAugmented == False`, exhaustive outcomes).
Whether they're worth taking is a separate question, see *Caveats* below.

The cross-venue scanner ran against the four NHL playoff games on tonight's
schedule. All four matched cleanly between Polymarket and Kalshi at
**−2.5% net edge** after fees — both venues priced lockstep, no arb.

## Install

Python 3.10+, no dependencies beyond the standard library. The scripts shell
out to `curl` for HTTP because every Python HTTP library on the planet has
slightly different timeout semantics and I got tired of debugging them.

```bash
git clone https://github.com/mearley24/polymarket-edge-scanner.git
cd polymarket-edge-scanner
mkdir -p reports
```

That's it. No pip, no virtualenv, no API keys.

## Run

```bash
# Same-venue: binary complement + multi-outcome field arb
python3 edge_hunt.py \
    --output reports/$(date -u +%Y%m%dT%H%M%SZ)-edge-hunt.md

# Cross-venue: Polymarket vs Kalshi mirror match
python3 cross_venue_arb_scan.py \
    --output reports/$(date -u +%Y%m%dT%H%M%SZ)-cross-venue.md

# Tighter thresholds
python3 edge_hunt.py --min-edge-pct 1.0 --min-depth-shares 50
python3 cross_venue_arb_scan.py --min-edge-pct 2.0 --series KXNBAGAME
```

Each script prints a ranked table to stdout and (with `--output`) writes a
markdown report. Run `--help` for full flags.

## Caveats — read these before you trade on anything

The scanners find **candidate** opportunities, not money. Several reasons a
flagged edge can fail to convert into a profit:

1. **Execution slippage on multi-leg fills.** A field arb across 8 candidates
   means 8 simultaneous orders. Other arbers are watching the same order
   book; by the time leg 5 fires, leg 1's quoted ask may have moved up. The
   scanner reports best-ask depth at snapshot time; real fills clip 30-60%
   of the headline edge in my experience.

2. **Polymarket fees aren't only 2%.** I assume 2% taker. Polymarket's
   schedule has occasionally added a "winner tax" on resolved positions
   (1-5% of profit). Verify the current fee schedule before sizing real
   trades.

3. **Capital lockup is the silent killer.** OpenAI IPO market settles
   Dec 31, 2026. If you deploy $30 there for a guaranteed +3.2%, that's
   ~5% annualized — barely better than a money market. The headline edge
   ignores time.

4. **The augmented filter is not foolproof.** I check `negRiskAugmented`
   from gamma's parent-event metadata. If a market is structured as
   non-augmented but has out-of-band ambiguity (e.g., the resolution rule
   leaves room for "no one wins"), my scanner flags it as a real arb when
   it isn't. Read each market's `rules_primary` before placing the trade.

5. **Cross-venue legs settle independently.** Polymarket pays in pUSD on a
   ~30-day settlement window. Kalshi pays in USD on resolution. If one leg
   refuses fills (HMAC errors, geoblocks, account flags), you're left with
   a directional position, not an arb.

6. **The scanner has no idea about your bankroll.** It reports edges
   regardless of whether the deployable size is $1 or $1000. Read
   `min_depth` carefully — a +9% edge with $1 of fillable size is a +$0.09
   trade after slippage, which is below most people's mental friction
   floor.

The +46% Nobel ghost was the cleanest possible reminder that "the math
checks out" is necessary but not sufficient. Always verify the structural
assumptions before deploying capital.

## What the scanners do NOT do

- **No order placement.** They surface candidates; you trade or you don't.
- **No bankroll management.** No position sizing, no Kelly fraction, no
  daily caps. Bring your own.
- **No alerting.** Run them on a cron and pipe the output where you want.
  I run mine via a daily scheduled job that pings me only when an edge
  clears a configurable threshold.
- **No "missing market" detection.** The augmented filter catches the most
  common false-positive pattern, but a deeply mispriced exhaustive market
  could still hide a real arb that this scanner skips.

## Why standard library + curl

Most arb tools I've seen pull in `httpx`, `aiohttp`, or `requests` plus a
half-dozen support libraries. For a script that lives on a personal box
and runs once a day, that's overhead I don't want. The scripts use
`subprocess.run(["curl", ...])` because curl is on every Unix box, has
predictable timeout behavior, and never fights me about TLS versions when
the user is running an old Python.

If you want async + connection pooling for thousands of markets, fork
freely — the API surfaces don't change.

## FAQ

### Does this actually place trades?

No. Both scripts are read-only — they hit Polymarket's gamma + CLOB endpoints
and Kalshi's public v2 API, compute edges, print a ranked table, and exit.
Wiring up execution is your problem, and I think you should think about it
twice before doing it. The caveats above are the reasons.

### Why poll instead of using Polymarket's WebSocket?

The scanner is a once-a-day cron tool, not a market-making bot. Polling 200
binaries via REST takes ~30 seconds with concurrency=12 and gives a clean
snapshot. WebSocket subscription is the right tool for a strategy that
reacts in milliseconds, not for "tell me at 7am whether any arb exists."
If you're doing the latter, fork and rewrite — the data shapes are the
same.

### What about Manifold / PredictIt / Limitless / Smarkets?

Out of scope for this repo. Manifold is mostly play money. PredictIt has an
$850/contract ceiling and a 5% withdrawal fee that eats most cross-venue
spreads. Limitless is on Polygon and might be a future addition; the
scanner architecture (gamma-style market list + per-token CLOB-style
orderbook) maps cleanly. Smarkets is UK-only and harder to access for US
users. PRs welcome.

### How often do real field arbs actually appear?

On the night I built this, the scanner found three real ones (after the
augmented filter): Trump 2026 gold-card count +9.5%, OpenAI IPO market cap
+3.2%, Harvey Weinstein sentencing +1.8%. Per-leg orderbook depth ranged
from 6 to 297 shares — meaning two of the three are uneconomic to fill
once you account for slippage. Run the scanner for a week before drawing
conclusions about frequency. I plan to publish a tracking dataset once
I have enough samples; if you fork and run it daily, share your data.

### What does `negRiskAugmented` actually mean under the hood?

Polymarket's NegRisk markets are mutually-exclusive (exactly one outcome
resolves YES). When `negRiskAugmented: True`, the protocol can mint an
implicit "Other" position automatically if the resolution doesn't match
any listed candidate — the named candidate markets all settle NO and the
"Other" claim accrues to the protocol's auto-generated outcome (you
can't trade it pre-resolution). Result: sum of YES asks across listed
candidates is by construction less than 1 even when the market is fairly
priced, which makes augmented events look like guaranteed arbitrages
when they aren't. The scanner skips these.

### Is this safe / against ToS?

The scanner is read-only against public endpoints — same calls polymarket.com
and kalshi.com make to render their own market pages. No auth needed, no
rate-limit issues at this volume, no ToS concern. **Trading from
restricted regions** (US Polymarket, certain Kalshi markets) is your
problem and is outside this repo's scope.

### Can I use this for a different kind of arb (sports books, DEXs, etc.)?

The Polymarket / Kalshi data shapes are specific to those venues, but
the math (`compute_cross_venue` pairing logic, `_align_poly_to_kalshi`
outcome alignment, the augmented filter) generalizes. Fork and swap the
fetch functions; happy to review a PR that adds a new venue cleanly.

### Why Python and not Go/Rust?

Honestly, this is a script that runs once or twice a day, finishes in
under a minute, and compiles a ranked list a human reads. Python's
standard library + curl is the lowest-friction path to "clone, run,
forget." If someone forks this into a Go binary that subscribes to
Polymarket WebSocket and reacts in <100ms, that's a different (more
useful for a real trader) tool — go do it.

## License

MIT. See `LICENSE`.

## Companion projects

I wrote a few related Polymarket utilities while building these scanners
that aren't in this repo. If there's interest I'll publish:

- A CLOB v2 / pUSD migration walkthrough (the on-chain `setApprovalForAll`
  + `approve(MAX_UINT256)` flow that current py-clob-client tutorials
  don't cover).
- A trading-bot starter kit with HMAC-isolated remote execution, geoblock
  pre-checks, and observer-only mode for paper trading.

Open an issue with what you'd find useful and I'll prioritize accordingly.

## Contributing

Issues and PRs welcome. Keep the standard-library-only constraint please —
the value of this repo is partly that you can clone it and run it without
fighting a dependency tree.
