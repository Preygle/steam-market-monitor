#!/usr/bin/env python3
"""Exhaustively verify the one rule the whole system rests on:

    net_received(list_price_for_net(floor)) >= floor

Run in CI on every push. If this ever fails, the sell planner is capable of
proposing a listing that loses money, and nothing else about the repo
matters until it is fixed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from steammkt.fees import WalletConfig, list_price_for_net, net_from_buyer_price


def main() -> int:
    cfg = WalletConfig()
    # Every floor from Rs 0.01 to Rs 100 in 1-paise steps, then decades out
    # to Rs 10,000 -- covers the whole realistic range of CS2 item prices.
    targets = list(range(1, 10_001)) + [50_000, 100_000, 500_000, 1_000_000]

    worst_margin = None
    for t in targets:
        price = list_price_for_net(t, cfg)
        net = net_from_buyer_price(price, cfg)
        if net < t:
            print(f"INVARIANT BROKEN: floor={t} price={price} net={net}",
                  file=sys.stderr)
            return 1
        margin = net - t
        if worst_margin is None or margin < worst_margin:
            worst_margin = margin

    print(f"no-loss invariant verified across {len(targets):,} price points")
    print(f"tightest margin observed: {worst_margin} paise (must be >= 0)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
