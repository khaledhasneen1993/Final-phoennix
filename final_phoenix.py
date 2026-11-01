# language: Python 3.11+
# Final Phoenix — Radical Risk Engine | Single File
# Take Profit = Final-3 style (1.8% → trail 0.9%)
# No averaging down | Smart Retreat | Revenge Mode | Survival Mode

import asyncio
import json
import time
import math
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict, deque
import logging

import ccxt.async_support as ccxt
import pandas as pd

# ====================== CONFIG ======================
LEVERAGE = 50
BASE_MARGIN_PCT = 0.09
MAX_MARGIN_PCT = 0.15
CASCADE_THRESHOLD = 0.012
VOLUME_SPIKE = 3.5
TAKE_PROFIT_PCT = 0.018          # نفس Final-3
TRAIL_DISTANCE = 0.009           # نفس Final-3
STOP_LOSS_PCT = 0.055            # أضيق شوي من Final-3
COOLDOWN_SEC = 10
SCAN_INTERVAL = 600
MAX_SYMBOLS = 40
MAX_POSITIONS = 4
MIN_QUOTE_VOLUME = 15_000_000
FEE_RATE = 0.0005
STATE_FILE = Path("paper_state_phoenix.json")

# Phoenix specific
RETREAT_LEVEL = 0.025            # عند -2.5% يقفل 50%
SURVIVAL_THRESHOLD = 0.85        # تحت 85% من أعلى رصيد → Survival
REVENGE_MIN_LOSS = 8.0           # خسارة أكبر من 8 USDT تفعل الانتقام
REVENGE_SIZE_MULT = 0.7          # حجم الانتقام
BLACKLIST_MINUTES = 45           # منع العملة بعد خسارتين متتاليتين

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

    def amount_to_precision(self, symbol, amount):
        return round(amount, 4)

    async def create_order(self, symbol, side, amount, reduce_only=False, reason=""):
        price = self.prices.get(symbol)
        if not price or price <= 0:
            raise ValueError("No valid price")
        amount = float(amount)
        notional = amount * price
        fee = notional * self.fee_rate

        if reduce_only:
            pos = self.positions.get(symbol)
            if not pos:
                raise ValueError("No position")
            direction = 1 if pos["side"] == "buy" else -1
            # partial or full close
            close_amount = min(amount, pos["amount"])
            pnl = (price - pos["entry"]) * close_amount * direction
            self.balance += pnl - fee
            pos["amount"] -= close_amount
            if pos["amount"] <= 1e-8:
                del self.positions[symbol]
            else:
                pos["margin"] = pos["amount"] * pos["entry"] / LEVERAGE
            trade = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": symbol, "side": side, "amount": close_amount,
                "price": price, "pnl_usdt": pnl, "fee_usdt": fee,
                "reduceOnly": True, "reason": reason
            }
            self.trades.append(trade)
            return trade

        # new entry
        margin = notional / LEVERAGE
        if margin + fee > self.free:
            raise ValueError("Insufficient margin")
        self.balance -= fee
        self.positions[symbol] = {
            "side": side, "entry": price, "amount": amount,
            "margin": margin, "original_entry": price
        }
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol, "side": side, "amount": amount,
            "price": price, "pnl_usdt": 0.0, "fee_usdt": fee,
            "reduceOnly": False, "reason": reason
        }
        self.trades.append(trade)
        return trade

# ====================== PHOENIX BOT ======================
class FinalPhoenix:
    def __init__(self):
        self.exchange = ccxt.binanceusdm({"enableRateLimit": True, "options": {"defaultType": "future"}})
        self.account = PaperAccount()
        self.positions = {}          # strategy view
        self.last_trade = {}
        self.blacklist = {}          # symbol → timestamp until
        self.recent_losses = defaultdict(list)  # symbol → list of loss amounts
        self.revenge_ready = False
        self.revenge_symbol = None
        self.survival_mode = False
        self.lock = asyncio.Lock()
        self.load_state()

    def load_state(self):
        if STATE_FILE.exists():
            try:
                data = json.loads(STATE_FILE.read_text())
                self.account.balance = data.get("balance", 100.0)
                self.account.peak_balance = data.get("peak_balance", self.account.balance)
                self.account.positions = data.get("account_positions", {})
                self.account.trades = data.get("trades", [])
                self.positions = data.get("strategy_positions", {})
                self.last_trade = data.get("last_trade", {})
                self.blacklist = data.get("blacklist", {})
                log.info(f"Phoenix loaded | balance={self.account.balance:.2f} | peak={self.account.peak_balance:.2f}")
            except Exception as e:
                log.warning(f"State load failed: {e}")

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
            "trades": self.account.trades[-120:],
            "last_trade": self.last_trade,
            "blacklist": self.blacklist,
            "updated": datetime.now(timezone.utc).isoformat()
        }
        STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    def is_blacklisted(self, symbol):
        until = self.blacklist.get(symbol, 0)
        return time.time() < until

    def update_blacklist(self, symbol, lost):
        self.recent_losses[symbol].append(lost)
        self.recent_losses[symbol] = self.recent_losses[symbol][-3:]
        if len(self.recent_losses[symbol]) >= 2 and sum(self.recent_losses[symbol][-2:]) < -10:
            self.blacklist[symbol] = time.time() + BLACKLIST_MINUTES * 60
            log.warning(f"BLACKLIST {symbol} for {BLACKLIST_MINUTES} min")

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
        except:
            return 0.0

    async def detect(self, symbol):
        if self.is_blacklisted(symbol):
            return None
        try:
            ohlcv = await self.exchange.fetch_ohlcv(symbol, "1m", limit=30)
            df = pd.DataFrame(ohlcv, columns=["ts", "o", "h", "l", "c", "v"])
            ret = (df["c"].iloc[-1] - df["c"].iloc[-3]) / df["c"].iloc[-3]
            vol_avg = df["v"].iloc[-15:-3].mean()
            vol_spike = df["v"].iloc[-1] / vol_avg if vol_avg > 0 else 0
            imb = await self.get_imbalance(symbol)

            long_sig = ret > CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb > 0.35
            short_sig = ret < -CASCADE_THRESHOLD and vol_spike > VOLUME_SPIKE and imb < -0.35
            strength = abs(ret) * vol_spike * abs(imb)

            return {
                "long": long_sig, "short": short_sig,
                "ret": ret, "vol_spike": vol_spike, "imb": imb,
                "strength": strength, "price": float(df["c"].iloc[-1]),
                "symbol": symbol
            }
        except:
            return None

    def calc_margin_pct(self, strength):
        if self.survival_mode:
            return BASE_MARGIN_PCT * 0.35
        if strength > 0.12:
            return min(MAX_MARGIN_PCT, BASE_MARGIN_PCT * 1.55)
        return BASE_MARGIN_PCT

    async def enter(self, symbol, side, price, strength, reason="CASCADE"):
        now = time.time()
        if symbol in self.last_trade and now - self.last_trade[symbol] < COOLDOWN_SEC:
            return False
        if len(self.positions) >= MAX_POSITIONS:
            return False
        if self.is_blacklisted(symbol):
            return False

        margin_pct = self.calc_margin_pct(strength)
        amount = (self.account.balance * margin_pct * LEVERAGE) / price
        amount = float(self.exchange.amount_to_precision(symbol, amount))
        if amount <= 0:
            return False

        try:
            self.account.prices[symbol] = price
            await self.account.create_order(symbol, side, amount, reason=reason)
            self.last_trade[symbol] = now
            self.positions[symbol] = {
                "side": side,
                "entry": price,
                "amount": amount,
                "sl": price * (1 - STOP_LOSS_PCT) if side == "buy" else price * (1 + STOP_LOSS_PCT),
                "tp": price * (1 + TAKE_PROFIT_PCT) if side == "buy" else price * (1 - TAKE_PROFIT_PCT),
                "best_price": price,
                "trailing": False,
                "retreated": False,
                "strength": strength,
                "margin_pct": margin_pct
            }
            log.info(f"ENTER {side.upper()} {symbol} | size={amount:.4f} @ {price:.4f} | {reason} | strength={strength:.3f}")
            self.save_state()
            return True
        except Exception as e:
            log.error(f"Enter failed {symbol}: {e}")
            return False

    async def close_partial(self, symbol, fraction, reason):
        p = self.positions.get(symbol)
        if not p:
            return
        amount = p["amount"] * fraction
        side = "sell" if p["side"] == "buy" else "buy"
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            price = ticker["last"]
            self.account.prices[symbol] = price
            await self.account.create_order(symbol, side, amount, reduce_only=True, reason=reason)
            p["amount"] *= (1 - fraction)
            if p["amount"] < 1e-6:
                del self.positions[symbol]
            else:
                # move SL closer after retreat
                direction = 1 if p["side"] == "buy" else -1
                p["sl"] = p["entry"] * (1 - 0.012 * direction)  # tighter after retreat
            log.info(f"RETREAT {reason} {symbol} | closed {fraction*100:.0f}% @ {price:.4f}")
            self.save_state()
        except Exception as e:
            log.warning(f"Partial close failed {symbol}: {e}")

    async def close_full(self, symbol, reason, price):
        p = self.positions.get(symbol)
        if not p:
            return
        side = "sell" if p["side"] == "buy" else "buy"
        try:
            self.account.prices[symbol] = price
            trade = await self.account.create_order(symbol, side, p["amount"], reduce_only=True, reason=reason)
            pnl = trade.get("pnl_usdt", 0)
            if pnl < -REVENGE_MIN_LOSS:
                self.revenge_ready = True
                self.revenge_symbol = symbol
                self.update_blacklist(symbol, pnl)
                log.warning(f"REVENGE ARMED after loss {pnl:.2f} on {symbol}")
            del self.positions[symbol]
            log.info(f"EXIT {reason} {symbol} @ {price:.4f} | pnl={pnl:.2f}")
            self.save_state()
        except Exception as e:
            log.error(f"Full close failed {symbol}: {e}")

    async def manage_positions(self):
        for symbol in list(self.positions.keys()):
            try:
                ticker = await self.exchange.fetch_ticker(symbol)
                price = ticker["last"]
                self.account.prices[symbol] = price
                p = self.positions[symbol]
                direction = 1 if p["side"] == "buy" else -1
                move = direction * (price / p["entry"] - 1)

                # 1. Hard Stop
                if direction * (price - p["sl"]) <= 0:
                    await self.close_full(symbol, "SL", price)
                    continue

                # 2. Smart Retreat at -2.5%
                if not p["retreated"] and move <= -RETREAT_LEVEL:
                    await self.close_partial(symbol, 0.50, "SMART_RETREAT")
                    p["retreated"] = True
                    continue

                # 3. Trailing (same as Final-3)
                if not p["trailing"] and move >= TAKE_PROFIT_PCT:
                    p["trailing"] = True
                    p["best_price"] = price

                if p["trailing"]:
                    if direction == 1:
                        p["best_price"] = max(p["best_price"], price)
                        new_sl = p["best_price"] * (1 - TRAIL_DISTANCE)
                        p["sl"] = max(p["sl"], new_sl)
                    else:
                        p["best_price"] = min(p["best_price"], price)
                        new_sl = p["best_price"] * (1 + TRAIL_DISTANCE)
                        p["sl"] = min(p["sl"], new_sl)

                self.save_state()
            except Exception as e:
                log.warning(f"Manage error {symbol}: {e}")

    async def try_revenge(self):
        if not self.revenge_ready or not self.revenge_symbol:
            return
        symbol = self.revenge_symbol
        # look for strong opposite signal on same symbol
        sig = await self.detect(symbol)
        if not sig:
            return
        # reverse of last loss direction is preferred but we take any strong signal
        if sig["long"] or sig["short"]:
            side = "buy" if sig["long"] else "sell"
            # smaller size for revenge
            old_max = MAX_MARGIN_PCT
            # temporarily force smaller size
            strength = sig["strength"] * 0.8
            await self.enter(symbol, side, sig["price"], strength, reason="REVENGE")
            self.revenge_ready = False
            self.revenge_symbol = None

    async def check_survival(self):
        peak = self.account.peak_balance
        if peak <= 0:
            return
        ratio = self.account.balance / peak
        if ratio < SURVIVAL_THRESHOLD and not self.survival_mode:
            self.survival_mode = True
            log.warning(f"SURVIVAL MODE ON | balance {self.account.balance:.1f} < {SURVIVAL_THRESHOLD*100:.0f}% of peak {peak:.1f}")
        elif ratio > 0.93 and self.survival_mode:
            self.survival_mode = False
            log.info("SURVIVAL MODE OFF")

    async def scan_cycle(self):
        await self.check_survival()
        symbols = await self.get_top_symbols()
        log.info(f"Scanning {len(symbols)} | positions={len(self.positions)} | survival={self.survival_mode}")

        signals = []
        for sym in symbols:
            sig = await self.detect(sym)
            if sig and (sig["long"] or sig["short"]):
                signals.append(sig)
            await asyncio.sleep(0.12)

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
            eq = self.account.equity
            log.info(f"STATUS | bal={self.account.balance:.2f} | eq={eq:.2f} | free={self.account.free:.2f} | pos={len(self.positions)} | peak={self.account.peak_balance:.2f}")

    async def run(self):
        log.info("Final Phoenix started | No averaging | Smart Retreat | Revenge | Survival")
        asyncio.create_task(self.exit_watcher())
        asyncio.create_task(self.status_loop())
        while True:
            try:
                await self.scan_cycle()
            except Exception as e:
                log.error(f"Cycle error: {e}")
            await asyncio.sleep(SCAN_INTERVAL)

async def main():
    bot = FinalPhoenix()
    try:
        await bot.run()
    except KeyboardInterrupt:
        bot.save_state()
        log.info("Phoenix stopped & saved")

if __name__ == "__main__":
    asyncio.run(main())
