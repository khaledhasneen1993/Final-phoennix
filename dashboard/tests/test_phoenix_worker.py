import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker
from ledger import round_trips

spec = importlib.util.spec_from_file_location(
    "phoenix_adapter_test", Path(__file__).resolve().parents[2] / "final_phoenix.py"
)
phoenix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phoenix)
SYMBOL = "BTC/USDT:USDT"


class Market:
    def __init__(self):
        self.price = 100

    async def fetch_tickers(self):
        return {SYMBOL: {"quoteVolume": 20_000_000}}

    async def fetch_ticker(self, symbol):
        return {"last": self.price, "timestamp": time.time() * 1000}

    async def fetch_order_book(self, symbol, limit):
        return {"bids": [[100, 3]], "asks": [[101, 1]]}

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        rows = [[n, 100, 103, 99, 100, 10] for n in range(limit)]
        rows[-1] = [limit, 100, 103, 99, 102, 50]
        return rows

    def amount_to_precision(self, symbol, amount):
        return round(amount, 8)


class PhoenixAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.market = Market()
        self.state = patch.object(
            phoenix, "STATE_FILE", Path(self.tmp.name) / "state.json"
        )
        self.state.start()
        with patch.object(phoenix.ccxt, "binanceusdm", return_value=self.market):
            self.bot = phoenix.FinalPhoenix()
        self.health = {}
        worker.observe_phoenix(self.bot, phoenix, self.health)
        self.settings = worker.phoenix_settings(phoenix)

    async def asyncTearDown(self):
        self.state.stop()
        self.tmp.cleanup()

    async def test_fresh_snapshot_is_100_with_no_trades_or_positions(self):
        s = worker.snapshot_state(
            "phoenix", self.bot, self.settings, "starting", self.health
        )
        self.assertEqual(s["balance_usdt"], 100)
        self.assertEqual(s["equity_usdt"], 100)
        self.assertFalse(s["trades"])
        self.assertFalse(s["account_positions"])
        self.assertEqual(s["settings"]["MAX_MARGIN_PCT"], 0.15)
        self.assertEqual(s["settings"]["add_levels"], [])
        json.dumps(s, allow_nan=False)

    async def test_scan_diagnostics_serialize_and_record_entry(self):
        with patch.object(phoenix.asyncio, "sleep", new=AsyncMock()):
            await self.bot.scan_cycle()
        s = worker.snapshot_state(
            "phoenix", self.bot, self.settings, "running", self.health
        )
        json.dumps(s, allow_nan=False)
        self.assertEqual(s["scan_report"]["phase"], "waiting")
        self.assertEqual(s["scan_report"]["checked"], 1)
        self.assertEqual(s["scan_report"]["successful"], 1)
        self.assertEqual(s["scan_report"]["signals"], 1)
        self.assertEqual(s["scan_report"]["entries"], 1)
        self.assertTrue(s["scanner"]["symbol_health"][0]["fresh"])
        self.assertIn("strength", s["trades"][0])

    async def test_rest_data_not_reported_fresh_without_timestamp(self):
        self.health[SYMBOL] = dict(received=time.time(), event=None)
        s = worker.snapshot_state(
            "phoenix", self.bot, self.settings, "running", self.health
        )
        self.assertEqual(s["scanner"]["fresh_symbols"], 0)
        self.assertIsNone(s["scanner"]["symbol_health"][0]["event_age"])

    async def test_raw_phoenix_retreat_and_stop_reasons_survive_aggregation(self):
        await self.bot.enter(SYMBOL, "buy", 100, 0.1)
        self.market.price = 97
        await self.bot.manage_positions()
        opened, closed, unmatched = round_trips(self.bot.account.trades)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["partials"], 1)
        self.assertFalse(unmatched)
        self.market.price = 93
        await self.bot.manage_positions()
        opened, closed, unmatched = round_trips(self.bot.account.trades)
        self.assertFalse(opened)
        self.assertEqual(closed[0]["exit_reason"], "SL")
        self.assertEqual(closed[0]["entry_reason"], "CASCADE")
        self.assertEqual(closed[0]["partials"], 1)
        self.assertAlmostEqual(
            closed[0]["net_pnl_usdt"], self.bot.account.balance - 100, places=3
        )

    async def test_failed_detection_visible_as_failure_not_no_signal(self):
        with patch.object(
            self.market, "fetch_ohlcv", side_effect=RuntimeError("offline")
        ):
            with patch.object(phoenix.asyncio, "sleep", new=AsyncMock()):
                await self.bot.scan_cycle()
        self.assertEqual(self.bot.dashboard_scan["failed"], 1)
        self.assertEqual(self.bot.dashboard_scan["checks"][SYMBOL]["status"], "error")
        self.assertFalse(self.bot.account.positions)

    async def test_repeated_scan_retains_total_and_separate_refresh_schedule(self):
        with patch.object(
            self.market, "fetch_tickers", wraps=self.market.fetch_tickers
        ) as top:
            await self.bot.scan_cycle()
            first_refresh = self.bot.next_universe_at
            await self.bot.scan_cycle()
        self.assertEqual(top.await_count, 1)
        report = self.bot.dashboard_scan
        self.assertEqual(report["mode"], "continuous_rest")
        self.assertEqual(report["total"], 1)
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["next_universe_at"], first_refresh)
        self.assertAlmostEqual(
            report["next_scan_at"] - report["finished_at"], 2, places=2
        )
        self.assertEqual(self.settings["watch_mode"], "continuous_rest")
        self.assertEqual(self.settings["universe_refresh"], 600)
        self.assertEqual(self.settings["scan_concurrency"], 4)
        json.dumps(report, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
