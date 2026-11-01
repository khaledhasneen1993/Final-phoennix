"""Durable fills and partial-aware round trips. Missing history stays explicitly incomplete."""

import hashlib
import json
import math
import sqlite3


def fill_key(fill):
    core = {
        k: fill.get(k)
        for k in ("timestamp", "symbol", "side", "amount", "price", "reduceOnly")
    }
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


class FillArchive:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS fills (key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.commit()

    def sync(self, fills):
        rows = [
            (fill_key(f), json.dumps(f, ensure_ascii=False, allow_nan=False))
            for f in fills
        ]
        self.db.executemany(
            "INSERT INTO fills VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
            rows,
        )
        self.db.commit()

    def read(self):
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT payload FROM fills ORDER BY rowid")
        ]

    def close(self):
        self.db.close()


def read_archive(path):
    if not path.is_file():
        return []
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    try:
        return [
            json.loads(row[0])
            for row in db.execute("SELECT payload FROM fills ORDER BY rowid")
        ]
    finally:
        db.close()


def round_trips(fills):
    opened, closed, unmatched = {}, [], []
    for fill in fills:
        symbol = fill["symbol"]
        amount, price = float(fill["amount"]), float(fill["price"])
        if not fill.get("reduceOnly"):
            if symbol not in opened:
                opened[symbol] = {
                    "symbol": symbol,
                    "side": fill["side"],
                    "entry_time": fill.get("timestamp"),
                    "fills": [],
                    "amount": 0.0,
                    "remaining_amount": 0.0,
                    "cost": 0.0,
                    "fees_usdt": 0.0,
                    "gross_pnl_usdt": 0.0,
                    "adds": 0,
                    "partials": 0,
                    "history_complete": not bool(
                        fill.get("scale_in") or fill.get("scaleIn")
                    ),
                    "entry_reason": fill.get("entry_reason") or fill.get("reason"),
                    "entry_signal": fill.get("entry_signal"),
                    "strength": fill.get("strength"),
                    "margin_pct": fill.get("margin_pct"),
                }
            p = opened[symbol]
            old_remaining = p["remaining_amount"]
            p["average_entry"] = (
                p.get("average_entry", 0) * old_remaining + price * amount
            ) / (old_remaining + amount)
            p["remaining_amount"] += amount
            p["amount"] += amount
            p["cost"] += amount * price
            p["adds"] += bool(fill.get("scale_in") or fill.get("scaleIn"))
        else:
            p = opened.get(symbol)
            if p is None:
                unmatched.append(fill)
                continue
            if amount > p["remaining_amount"] and not math.isclose(
                amount, p["remaining_amount"], rel_tol=1e-8
            ):
                p["history_complete"] = False
            p["remaining_amount"] = max(0.0, p["remaining_amount"] - amount)
            p["gross_pnl_usdt"] += float(fill.get("pnl_usdt", 0))
            p["exit_time"], p["exit_price"] = fill.get("timestamp"), price
            p["exit_reason"] = fill.get("exit_reason") or fill.get("reason")
            if fill.get("partial_close") or p["remaining_amount"] > 1e-9:
                p["partials"] += 1
            else:
                p["remaining_amount"] = 0.0
                opened.pop(symbol)
                closed.append(p)
        p["fees_usdt"] += float(fill.get("fee_usdt", 0))
        p["net_pnl_usdt"] = p["gross_pnl_usdt"] - p["fees_usdt"]
        p["fills"].append(fill)
    return list(opened.values()), closed, unmatched
