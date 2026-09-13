"""Import a Steam inventory into `holdings`, with cost basis attached."""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from .client import SteamClient
from .costbasis import CostModel, classify_item
from .store import Store

# Which collections/sets count as "the newest Armory batch". Items from
# these get an Armory cost basis; anything else is tagged source='other'
# and given a zero cost basis so it can never drag the P/L calculation.
ARMORY_2026_07 = {
    "The Arabesque Collection",
    "The Spy Tech Collection",
    "Fruits And Veggies Stickers",
    "Auto Racing Stickers",
}


def _tag(desc: dict, category: str) -> Optional[str]:
    for t in desc.get("tags", []) or []:
        if t.get("category") == category:
            return t.get("localized_tag_name") or t.get("name")
    return None


def _collection(desc: dict) -> Optional[str]:
    # Steam exposes the collection in the description blocks.
    for d in desc.get("descriptions", []) or []:
        v = (d.get("value") or "").strip()
        if v.endswith("Collection") or v.endswith("Stickers"):
            return v
    return _tag(desc, "Collection")


def import_inventory(store: Store, client: SteamClient, steamid64: str,
                     cost: CostModel, only_armory: bool = True) -> dict:
    """Pull the public inventory and write holdings with cost basis."""
    raw = client.inventory(steamid64)
    if not raw:
        return {"error": "inventory empty or private",
                "hint": "Profile > Edit Profile > Privacy > Inventory: Public"}

    counts = {"total": len(raw), "imported": 0, "skipped": 0, "by_type": {}}
    now = dt.datetime.now().isoformat(timespec="seconds")

    with store.tx() as c:
        for a in raw:
            desc = a.get("_desc", {})
            name = desc.get("market_hash_name")
            if not name:
                counts["skipped"] += 1
                continue
            if not desc.get("marketable", 0):
                counts["skipped"] += 1
                continue

            coll = _collection(desc)
            itype = classify_item(name, desc)
            in_batch = (coll in ARMORY_2026_07) if coll else False

            if only_armory and not in_batch:
                counts["skipped"] += 1
                continue

            basis = cost.item_cost_paise(itype) if in_batch else 0

            c.execute(
                "INSERT OR REPLACE INTO holdings(asset_id,market_hash_name,"
                "class_id,instance_id,item_type,rarity,exterior,tradable,"
                "marketable,tradable_after,source,star_cost,cost_basis_paise,"
                "acquired_at,raw) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a["assetid"], name, a.get("classid"), a.get("instanceid"),
                 itype, _tag(desc, "Rarity"), _tag(desc, "Exterior"),
                 desc.get("tradable", 0), desc.get("marketable", 0),
                 desc.get("cache_expiration"), coll or "other",
                 None, basis, now, json.dumps(desc)[:20000]),
            )
            counts["imported"] += 1
            counts["by_type"][itype] = counts["by_type"].get(itype, 0) + 1

    return counts
