# language: Python 3.11+
# Final Phoenix v2 — Fixed & Hardened
# No averaging | Smart Retreat | Proper Revenge | Survival | Full state

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
import logging

import ccxt.async_support as ccxt
import pandas as pd

# ====================== CONFIG ======================
LEVERAGE = 50
BASE_MARGIN_PCT = 0.09
MAX_MARGIN_PCT = 0.15
CASCADE_THRESHOLD = 0.012
VOLUME_SPIKE = 3.5
TAKE_PROFIT_PCT = 0.018
TRAIL_DISTANCE = 0.009
STOP_LOSS_PCT = 0.055
COOLDOWN_SEC = 10
SCAN_INTERVAL = 600
MAX_SYMBOLS = 40
MAX_POSITIONS = 4
MIN_QUOTE_VOLUME = 15_000_000
FEE_RATE = 0.0005
STATE_FILE = Path("paper_state_phoenix.json")

RETREAT_LEVEL = 0.025
SURVIVAL_THRESHOLD = 0.85
REVENGE_MIN_LOSS = 8.0
REVENGE_SIZE_MULT = 0.65
BLACKLIST_MINUTES = 45
MAX_TICKER_AGE = 12.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("PHOENIX")


# ====================== PAPER ACCOUNT ======================
class PaperAccount:
    def __init__(self):
        self.balance = 100.0
        self.peak_balance = 100.0
        self.positions = {}
        self.prices = {}
        self.trades = []
        self.fee_rate = FEE_RATE

    @property
    def used(self):
        return sum(p.get("margin", 0) for p in self.positions.values())

    @property
    def free(self):
        return max(0.0, self.balance - self.used)

    @property
    def equity(self):
        pnl = 0.0
        for s, p in self.positions.items():
            price = self.prices.get(s, p["entry"])
            direction = 1 if p["side"] == "buy" else -1
            pnl += (price - p["entry"]) * p["amount"] * direction
        return self.balance + pnl

    async def create_order(self, symbol, side, amount, reduce_only=False, reason=""):
        price = self.prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError("No valid price")
        amount = float(amount)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError("Invalid amount")
        if side not in ("buy", "sell"):
            raise ValueError("Invalid side")

        if reduce_only:
            pos = self.positions.get(symbol)
            if not pos:
                raise ValueError("No position")
            if side == pos["side"]:
                raise ValueError("Closing side must oppose position")
            direction = 1 if pos["side"] == "buy" else -1
            close_amount = min(amount, pos["amount"])
            fee = close_amount * price * self.fee_rate
            pnl = (price - pos["entry"]) * close_amount * direction
            self.balance += pnl - fee
            pos["amount"] -= close_amount
            if pos["amount"] <= 1e-8:
                del self.positions[symbol]
            else:
                pos["margin"] = pos["amount"] * pos["entry"] / LEVERAGE
            trade = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol,
                "side": side,
                "amount": close_amount,
                "price": price,
                "pnl_usdt": round(pnl, 4),
                "fee_usdt": round(fee, 4),
                "reduceOnly": True,
                "reason": reason,
            }
            self.trades.append(trade)
            return trade

        if symbol in self.positions:
            raise ValueError("Position already exists")
        notional = amount * price
        fee = notional * self.fee_rate
        margin = notional / LEVERAGE
        if margin + fee > self.free:
            raise ValueError("Insufficient margin")
        self.balance -= fee
        self.positions[symbol] = {
            "side": side,
            "entry": price,
            "amount": amount,
            "margin": margin,
            "original_entry": price,
        }
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "side": side,
            "amount": amount,
            "price": price,
            "pnl_usdt": 0.0,
            "fee_usdt": round(fee, 4),
            "reduceOnly": False,
            "reason": reason,
        }
        self.trades.append(trade)
        return trade


# ====================== PHOENIX BOT ======================
class FinalPhoenix:
    def __init__(self):
        self.account = PaperAccount()
        self.positions = {}
        self.last_trade = {}
        self.blacklist = {}
        self.recent_losses = defaultdict(list)
        self.revenge_ready = False
        self.revenge_symbol = None
        self.revenge_side = None
        self.survival_mode = False
        self.lock = asyncio.Lock()
        self.load_state()
        self.exchange = ccxt.binanceusdm(
            {"enableRateLimit": True, "options": {"defaultType": "future"}}
        )

    def load_state(self):
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.account.balance = data.get("balance", 100.0)
            self.account.peak_balance = data.get("peak_balance", self.account.balance)
            self.account.positions = data.get("account_positions", {})
            self.account.trades = data.get("trades", [])
            self.positions = data.get("strategy_positions", {})
            self.last_trade = data.get("last_trade", {})
            self.blacklist = data.get("blacklist", {})
            self.recent_losses = defaultdict(
                list, {k: v for k, v in data.get("recent_losses", {}).items()}
            )
            self.revenge_ready = data.get("revenge_ready", False)
            self.revenge_symbol = data.get("revenge_symbol")
            self.revenge_side = data.get("revenge_side")
            self.survival_mode = data.get("survival_mode", False)
            log.info(
                f"Phoenix loaded | bal={self.account.balance:.2f} | peak={self.account.peak_balance:.2f}"
            )
        except Exception as e:
            raise RuntimeError(
                f"Cannot load state {STATE_FILE}; original file preserved"
            ) from e

    def save_state(self):
        self.account.peak_balance = max(self.account.peak_balance, self.account.balance)
        data = {
            "balance": self.account.balance,
            "peak_balance": self.account.peak_balance,
            "equity": self.account.equity,
            "free": self.account.free,
            "used": self.account.used,
            "account_positions": self.account.positions,
            "strategy_positions": self.positions,
            "trades": self.account.trades[-150:],
            "last_trade": self.last_trade,
            "blacklist": self.blacklist,
            "recent_losses": dict(self.recent_losses),
            "revenge_ready": self.revenge_ready,
            "revenge_symbol": self.revenge_symbol,
            "revenge_side": self.revenge_side,
            "survival_mode": self.survival_mode,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        tmp.replace(STATE_FILE)

    def is_blacklisted(self, symbol):
        return time.time() < self.blacklist.get(symbol, 0)

    def update_blacklist(self, symbol, lost):
        self.recent_losses[symbol].append(lost)
        self.recent_losses[symbol] = self.recent_losses[symbol][-3:]
        if (
            len(self.recent_losses[symbol]) >= 2
            and sum(self.recent_losses[symbol][-2:]) < -10
        ):
            self.blacklist[symbol] = time.time() + BLACKLIST_MINUTES * 60
            log.warning(f"BLACKLIST {symbol} for {BLACKLIST_MINUTES} min")

    async def fresh_ticker(self, symbol):
        ticker = await self.exchange.fetch_ticker(symbol)
        price = float(ticker["last"])
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Invalid price")

        # محاولة تقدير عمر السعر
        ts = ticker.get("timestamp")
        if ts:
            ts = float(ts)
            if not math.isfinite(ts):
                raise ValueError("Invalid ticker timestamp")
            age = (time.time() * 1000 - ts) / 1000
            if age > MAX_TICKER_AGE:
                raise ValueError(f"Stale ticker age={age:.1f}s")
        return price

    async def get_top_symbols(self):
        try:
            tickers = await self.exchange.fetch_tickers()
            candidates = []
            for symbol, t in tickers.items():
                if not symbol.endswith(":USDT") or "/USDT:USDT" not in symbol:
                    continue
                vol = float(t.get("quoteVolume") or 0)
                if vol >= MIN_QUOTE_VOLUME:
                    candidates.append((symbol, vol))
            candidates.sort(key=lambda x: x[1], reverse=True)
            return [s for s, _ in candidates[:MAX_SYMBOLS]]
        except Exception as e:
            log.error(f"Top symbols error: {e}")
            return ["BTC/USDT:USDT", "ETH/USDT:USDT"]

    async def get_imbalance(self, symbol):
        try:
            ob = await self.exchange.fetch_order_book(symbol, limit=20)
            bid = sum(x[1] for x in ob["bids"][:20])
            ask = sum(x[1] for x in ob["asks"][:20])
            return (bid - ask) / (bid + ask) if (bid + ask) > 0 else 0.0
        except Exception as e:
            log.debug("Order book unavailable for %s: %s", symbol, e)
            return 0.0

    async def detect(self, symbol, allow_blacklisted=False):
        if self.is_blacklisted(symbol) and not allow_blacklisted:
            return None
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, "1m", limit=30)
            df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            ret = (df["c"].iloc[-1] - df["c"].iloc[-3]) / df["c"].iloc[-3]
            vol_avg = df["v"].iloc[-15:-3].mean()
            vol_spike = df["v"].iloc[-1] / vol_avg if vol_avg > 0 else 0
            imb = await self.get_imbalance(symbol)

            long_sig = (
                ret > CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb > 0.35
            )
            short_sig = (
                ret < -CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb < -0.35
            )
            strength = abs(ret) * vol_spike * abs(imb)

            return {
                "long": long_sig,
                "short": short_sig,
                "ret": ret,
                "vol_spike": vol_spike,
                "imb": imb,
                "strength": strength,
                "price": float(df["c"].iloc[-1]),
                "symbol": symbol,
            }
        except Exception as e:
            log.debug("Signal detection failed for %s: %s", symbol, e)
            return None

    def calc_margin_pct(self, strength, is_revenge=False):
        if self.survival_mode:
            return BASE_MARGIN_PCT * 0.30
        if is_revenge:
            return BASE_MARGIN_PCT * REVENGE_SIZE_MULT
        if strength > 0.11:
            return min(MAX_MARGIN_PCT, BASE_MARGIN_PCT + (strength - 0.11) * 0.8)
        return BASE_MARGIN_PCT

    async def enter(
        self, symbol, side, price, strength, reason="CASCADE", is_revenge=False
    ):
        now = time.time()
        if symbol in self.last_trade and now - self.last_trade[symbol] < COOLDOWN_SEC:
            return False
        if symbol in self.positions or symbol in self.account.positions:
            return False
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if self.is_blacklisted(symbol) and not is_revenge:
            return False

        try:
            price = await self.fresh_ticker(symbol)
            margin_pct = self.calc_margin_pct(strength, is_revenge=is_revenge)
            amount = (self.account.balance * margin_pct * LEVERAGE) / price
            amount = float(self.exchange.amount_to_precision(symbol, amount))
            if not math.isfinite(amount) or amount <= 0:
                return False

            self.account.prices[symbol] = price
            await self.account.create_order(symbol, side, amount, reason=reason)
            self.last_trade[symbol] = now
            self.positions[symbol] = {
                "side": side,
                "entry": price,
                "amount": amount,
                "sl": (
                    price * (1 - STOP_LOSS_PCT)
                    if side == "buy"
                    else price * (1 + STOP_LOSS_PCT)
                ),
                "best_price": price,
                "trailing": False,
                "retreated": False,
                "strength": strength,
                "margin_pct": margin_pct,
                "is_revenge": is_revenge,
            }
            log.info(
                f"ENTER {side.upper()} {symbol} | size={amount:.4f} @ {price:.4f} | {reason} | margin={margin_pct*100:.1f}%"
            )
            self.save_state()
            return True
        except Exception as e:
            log.error(f"Enter failed {symbol}: {e}")
            return False

    async def close_partial(self, symbol, fraction, reason):
        p = self.positions.get(symbol)
        if not p:
            return
        if not math.isfinite(fraction) or not 0 < fraction <= 1:
            raise ValueError("Invalid close fraction")
        amount = p["amount"] * fraction
        side = "sell" if p["side"] == "buy" else "buy"
        try:
            price = await self.fresh_ticker(symbol)
            self.account.prices[symbol] = price
            await self.account.create_order(
                symbol, side, amount, reduce_only=True, reason=reason
            )
            remaining = self.account.positions.get(symbol)
            if remaining is None:
                del self.positions[symbol]
            else:
                p["amount"] = remaining["amount"]
                # الوقف على مسافة 3.2% من سعر تنفيذ الانسحاب.
                direction = 1 if p["side"] == "buy" else -1
                p["sl"] = price * (1 - 0.032 * direction)
                p["retreated"] = True
            log.info(
                f"RETREAT {reason} {symbol} | closed {fraction*100:.0f}% @ {price:.4f}"
            )
            self.save_state()
        except Exception as e:
            log.warning(f"Partial close failed {symbol}: {e}")

    async def close_full(self, symbol, reason, price=None):
        p = self.positions.get(symbol)
        if not p:
            return
        side = "sell" if p["side"] == "buy" else "buy"
        try:
            if price is None:
                price = await self.fresh_ticker(symbol)
            self.account.prices[symbol] = price
            trade = await self.account.create_order(
                symbol, side, p["amount"], reduce_only=True, reason=reason
            )
            pnl = trade.get("pnl_usdt", 0.0)

            if pnl < -REVENGE_MIN_LOSS:
                self.revenge_ready = True
                self.revenge_symbol = symbol
                # عكس الاتجاه للانتقام
                self.revenge_side = "sell" if p["side"] == "buy" else "buy"
                self.update_blacklist(symbol, pnl)
                log.warning(
                    f"REVENGE ARMED | loss={pnl:.2f} | next side={self.revenge_side}"
                )

            del self.positions[symbol]
            log.info(f"EXIT {reason} {symbol} @ {price:.4f} | pnl={pnl:.2f}")
            self.save_state()
        except Exception as e:
            log.error(f"Full close failed {symbol}: {e}")

    async def manage_positions(self):
        for symbol in list(self.positions.keys()):
            try:
                price = await self.fresh_ticker(symbol)
                self.account.prices[symbol] = price
                p = self.positions[symbol]
                direction = 1 if p["side"] == "buy" else -1
                move = direction * (price / p["entry"] - 1)

                # Hard Stop
                if direction * (price - p["sl"]) <= 0:
                    await self.close_full(
                        symbol, "SL" if not p["trailing"] else "TRAIL_SL", price
                    )
                    continue

                # Smart Retreat
                if not p["retreated"] and move <= -RETREAT_LEVEL:
                    await self.close_partial(symbol, 0.50, "SMART_RETREAT")
                    continue

                # Trailing (Final-3 style)
                if not p["trailing"] and move >= TAKE_PROFIT_PCT:
                    p["trailing"] = True
                    p["best_price"] = price

                if p["trailing"]:
                    if direction == 1:
                        p["best_price"] = max(p["best_price"], price)
                        p["sl"] = max(p["sl"], p["best_price"] * (1 - TRAIL_DISTANCE))
                    else:
                        p["best_price"] = min(p["best_price"], price)
                        p["sl"] = min(p["sl"], p["best_price"] * (1 + TRAIL_DISTANCE))

                self.save_state()
            except Exception as e:
                log.warning(f"Manage error {symbol}: {e}")

    async def try_revenge(self):
        if not self.revenge_ready or not self.revenge_symbol or not self.revenge_side:
            return
        symbol = self.revenge_symbol
        side = self.revenge_side

        # نسمح بالاكتشاف حتى لو محظورة
        sig = await self.detect(symbol, allow_blacklisted=True)
        if sig and (
            (side == "buy" and sig["long"]) or (side == "sell" and sig["short"])
        ):
            success = await self.enter(
                symbol,
                side,
                sig["price"],
                sig["strength"] * 0.9,
                reason="REVENGE",
                is_revenge=True,
            )
            if success:
                # نمسح الجاهزية فقط بعد دخول ناجح
                self.revenge_ready = False
                self.revenge_symbol = None
                self.revenge_side = None
                self.save_state()
        # إذا ما نجح الدخول، تبقى الجاهزية موجودة للمحاولة الجاية

    async def check_survival(self):
        peak = max(self.account.peak_balance, 1.0)
        ratio = self.account.balance / peak
        if ratio < SURVIVAL_THRESHOLD and not self.survival_mode:
            self.survival_mode = True
            log.warning(
                f"SURVIVAL MODE ON | {self.account.balance:.1f} < {SURVIVAL_THRESHOLD*100:.0f}% of peak"
            )
            self.save_state()
        elif ratio > 0.93 and self.survival_mode:
            self.survival_mode = False
            log.info("SURVIVAL MODE OFF")
            self.save_state()

    async def scan_cycle(self):
        await self.check_survival()
        symbols = await self.get_top_symbols()
        log.info(
            f"Scan {len(symbols)} | pos={len(self.positions)} | survival={self.survival_mode}"
        )

        signals = []
        for sym in symbols:
            sig = await self.detect(sym)
            if sig and (sig["long"] or sig["short"]):
                signals.append(sig)
            await asyncio.sleep(0.11)

        signals.sort(key=lambda x: x["strength"], reverse=True)

        async with self.lock:
            await self.manage_positions()
            await self.try_revenge()
            for sig in signals:
                if len(self.positions) >= MAX_POSITIONS:
                    break
                if sig["symbol"] in self.positions:
                    continue
                side = "buy" if sig["long"] else "sell"
                await self.enter(sig["symbol"], side, sig["price"], sig["strength"])

    async def exit_watcher(self):
        while True:
            try:
                async with self.lock:
                    await self.manage_positions()
            except Exception as e:
                log.error(f"Exit watcher: {e}")
            await asyncio.sleep(7)

    async def status_loop(self):
        while True:
            await asyncio.sleep(25)
            log.info(
                f"STATUS | bal={self.account.balance:.2f} | eq={self.account.equity:.2f} | "
                f"free={self.account.free:.2f} | pos={len(self.positions)} | peak={self.account.peak_balance:.2f}"
            )

    async def run(self):
        log.info("Final Phoenix v2 started | Fixed Retreat + Revenge + Full State")
        watcher = asyncio.create_task(self.exit_watcher())
        status = asyncio.create_task(self.status_loop())
        try:
            while True:
                try:
                    await self.scan_cycle()
                except Exception as e:
                    log.error(f"Cycle error: {e}")
                await asyncio.sleep(SCAN_INTERVAL)
        finally:
            watcher.cancel()
            status.cancel()
            await asyncio.gather(watcher, status, return_exceptions=True)
            try:
                self.save_state()
            finally:
                await self.exchange.close()


async def main():
    bot = FinalPhoenix()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Phoenix stopped")
