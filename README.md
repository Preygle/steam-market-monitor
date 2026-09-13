# steam-market-monitor

Fee-exact price monitoring and sell planning for CS2 items on the Steam
Community Market, built around a single rule:

```
net_received(list_price) >= cost_basis        for every listed item
```

That check lives in `strategy.SellPlan.validate()`, it **raises** rather than
warns, and it runs again inside the monitor loop before any alert is emitted.
A bug in the prediction logic crashes the sweep. It cannot quietly propose a
listing that loses money. CI re-verifies the invariant across 10,004 price
points on every push.

---

## Why this exists

Most "is my skin worth selling?" arithmetic is wrong in the same three ways.

**1. The Steam fee is 13.0%, not 15%.**
Steam takes 5% and the publisher takes 10%, but both are levied on what the
*seller nets*, not on what the buyer pays. The real bite out of a listing is
15/115 = **13.04%**. Below roughly ₹10 the per-listing minimum fees are floored
to whole units and the effective rate climbs again.

| Buyer pays | You net | Fees | Effective |
|-----------:|--------:|-----:|----------:|
| ₹1 | 0.88 | 0.12 | 12.0% |
| ₹5 | 4.36 | 0.64 | 12.8% |
| ₹100 | 86.97 | 13.03 | 13.0% |
| ₹1,000 | 869.58 | 130.42 | 13.0% |

`steammkt/fees.py` is a direct port of Valve's own `CalculateFeeAmount`,
in integer minor units, so its numbers match the Sell dialog exactly. The
useful direction is the inverse — *given a floor I must clear, what price do I
type?* — and `list_price_for_net()` round-trips its own answer through the
forward function, walking the price up until it genuinely clears. Rounding can
only ever err in the seller's favour.

**2. Locale parsing silently destroys floors.**
`₹ 3,656` parsed naively becomes ₹3.656 — a 1000× understatement, and every
break-even floor derived from it is catastrophically low. The parser handles
Indian lakh grouping (`₹ 1,23,456.78`), euro-style separators (`1.234,56`) and
bare integers, and is regression-tested against all of them.

**3. A price with no buyers is not a price.**
The strategy gates on 24-hour volume before it will call anything sellable, and
the portfolio roll-up reports "NOT YET" when items can't clear — rather than
quietly excluding them to make the total look healthy.

## Armory cost basis

An Armory Pass yields at most 40 credits. Redemptions are fixed-price:

| Redemption | Credits |
|---|---:|
| Sticker set | 1 |
| Weapon case | 2 |
| Charm capsule | 3 |
| Collection skin | 4 |
| Limited edition | 25–125 |

So cost basis per item is just credit cost × what a credit actually cost you.
The divisor is **credits you actually earned**, not 40 — grinding a pass only
to 30 makes every credit a third more expensive, and every floor moves up with
it. `costbasis.py` rounds up, never down.

## Event-aware spike attribution

`config/events.yaml` carries 31 dated CS2 market events (2024–2026): case and
capsule releases, Armory rotations, map-pool changes, Majors, trade-policy
shocks and discontinuations. When the analyser sees a move past its threshold
it looks for an event in the window **and checks the direction matches** — so a
coincidence is reported as one rather than sold as a cause.

The events that carry the most signal:

- **2025-10-23** — trade-up expansion (5 Coverts → knife). Market cap fell
  ~28–30% in a day; knives and gloves −60–70%; Covert fodder ran 5–20×. Proof
  that CS2 reprices in *hours* on supply news.
- **2025-10-02** — Austin 2025 capsules pulled from sale. Prices roughly
  doubled within hours; the Paris 2023 precedent ran 3–4× within a year.
- **2026-05-22** — Major stickers moved to a token shop. A structural break:
  any model trained on pre-May capsule behaviour is invalid after it.

Armory items never enter the passive weekly drop pool, so their supply stops
growing once a rotation ends. That is the structural argument for patience, and
it is stronger than any seasonality estimate — the monthly bias table is
single-sourced and is only ever used to break ties, never to justify selling
below floor.

## What it does not do

| | |
|---|---|
| Monitor prices 24/7 | automated |
| Detect event-driven spikes | automated |
| Compute the exact list price | automated |
| Alert you (console / Windows toast / Telegram) | automated |
| Click Sell + Steam Guard confirm | **you** |

**It does not place listings.** Steam requires a mobile Steam Guard
confirmation for every market listing and there is no API for it; the only
bypass is extracting your authenticator's `identity_secret`, which hands full
trading control of your account to whatever holds it. That is not a trade worth
making, least of all to save ten seconds — and an unattended bot firing sell
orders at 3am is exactly how a "never lose money" guarantee breaks in practice.

## Install

```bash
git clone https://github.com/<you>/steam-market-monitor.git
cd steam-market-monitor
pip install -r requirements.txt
cp config/config.example.yaml config/config.yaml
```

Fill in `config/config.yaml`: your `steamid64`, the Armory pass price in paise,
how many passes you bought and how many credits you actually earned.

### Calibrate the fee constants first

```bash
python -m steammkt.cli calibrate --price 90     # expect: you receive ₹78.27
```

Open any item's Sell dialog on Steam, type the same price, compare
"You receive". They must match. If not, adjust `wallet.fee_minimum` and
`wallet.min_listing_price` until they do — every floor depends on it. Valve
changed per-currency minimum prices in December 2025, which pushed effective
fees far higher on minimum-price listings in some currencies.

## Usage

```bash
python -m steammkt.cli fees               # the fee table
python -m steammkt.cli costbasis          # your ₹/credit and per-item cost
python -m steammkt.cli import-inventory   # pull inventory into the db
python -m steammkt.cli fetch-history      # backfill daily price history
python -m steammkt.cli analyze            # attribute spikes to events
python -m steammkt.cli plan               # one-shot sell plan
python -m steammkt.cli monitor            # run forever
```

Set `steam.session_cookie` to unlock `/market/pricehistory/` (the full daily
series). Without it the monitor accumulates its own history from hourly
snapshots instead. Keep `requests_per_minute` at or below 12 — Steam starts
429-ing at around 20.

## Layout

```
steammkt/
  fees.py        exact port of Valve's fee arithmetic (integer minor units)
  client.py      rate-limited Steam client + locale-safe price parser
  store.py       SQLite; all state, safe to kill and restart mid-sweep
  costbasis.py   Armory passes -> ₹ per credit -> ₹ per item
  events.py      event calendar + direction-checked spike attribution
  strategy.py    the decision engine and the no-loss invariant
  inventory.py   inventory import
  monitor.py     the loop
  alerts.py      console / Windows toast / Telegram (output only)
  cli.py         entry point
config/
  events.yaml    31 dated CS2 market events, 2024-2026
scripts/
  verify_invariant.py   exhaustive no-loss check, run in CI
```

## Development

```bash
pip install -r requirements-dev.txt
pytest                                # 47 tests
python scripts/verify_invariant.py    # exhaustive invariant sweep
```

CI runs the suite on Python 3.10/3.11/3.12 and posts the result to Telegram.
To enable notifications, add two repository secrets under
**Settings → Secrets and variables → Actions**:

| Secret | Where from |
|---|---|
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | [@userinfobot](https://t.me/userinfobot) → send it any message |

Without them the notify step logs a notice and exits cleanly, so CI still
passes on forks.

## Caveats

- Fee minimums for non-USD currencies are assumed, not verified. Calibrate.
- Event dates marked low confidence in `events.yaml` could not be corroborated
  to an exact day. Seasonality magnitudes are single-sourced and weak.
- Thin-volume items (a few sales a day) have indicative prices, not firm ones.
- Nothing here places a listing, moves an item, or touches your credentials.

## Licence

MIT
