"""Verify Phoenix risk management, ticker freshness and revenge retries offline."""

import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, AsyncMock

spec = importlib.util.spec_from_file_location(
    "phoenix", Path(__file__).resolve().parents[1] / "final_phoenix.py"
)
phoenix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phoenix)
S = "BTC/USDT:USDT"


class PublicData:
    def __init__(self):
        self.price = 100

    def amount_to_precision(self, symbol, amount):
        return round(amount, 8)

    async def fetch_ticker(self, symbol):
        return {"last": self.price, "timestamp": 0}

    async def close(self):
        self.closed = True

    async def fetch_order_book(self, symbol, limit):
        return {"bids": [[100, 3]], "asks": [[101, 1]]}

    async def fetch_ohlcv(self, symbol, timeframe, limit):
        rows = [[n, 100, 103, 99, 100, 10] for n in range(limit)]
        rows[-1] = [limit, 100, 103, 99, 102, 50]
        return rows


class PhoenixTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.previous = phoenix.STATE_FILE
        phoenix.STATE_FILE = Path(self.tmp.name) / "state.json"
        self.data = PublicData()
        with patch.object(phoenix.ccxt, "binanceusdm", return_value=self.data):
            self.bot = phoenix.FinalPhoenix()

    async def asyncTearDown(self):
        phoenix.STATE_FILE = self.previous
        self.tmp.cleanup()

    async def test_default_account_and_actual_margin_formula(self):
        self.assertEqual(self.bot.account.balance, 100)
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.1), 0.09)
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.2), 0.15)
        self.bot.survival_mode = True
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.2), 0.027)

    async def test_original_signal_thresholds(self):
        signal = await self.bot.detect(S)
        self.assertTrue(signal["long"])
        self.assertFalse(signal["short"])
        self.assertAlmostEqual(signal["strength"], 0.02 * 5 * 0.5)
        self.bot.blacklist[S] = time.time() + 60
        self.assertIsNone(await self.bot.detect(S))

    async def test_hard_stop_precedes_retreat(self):
        self.assertTrue(await self.bot.enter(S, "buy", 100, 0.1))
        self.data.price = 94
        await self.bot.manage_positions()
        self.assertNotIn(S, self.bot.positions)
        self.assertNotIn(S, self.bot.account.positions)
        self.assertEqual(len(self.bot.account.trades), 2)
        self.assertEqual(self.bot.account.trades[-1]["reason"], "SL")

    async def test_retreat_halves_and_keeps_remainder_at_same_price(self):
        await self.bot.enter(S, "buy", 100, 0.1)
        amount = self.bot.positions[S]["amount"]
        self.data.price = 97
        await self.bot.manage_positions()
        p = self.bot.positions[S]
        self.assertTrue(p["retreated"])
        self.assertAlmostEqual(p["amount"], amount / 2)
        self.assertAlmostEqual(self.bot.account.positions[S]["amount"], amount / 2)
        self.assertAlmostEqual(p["sl"], 97 * (1 - 0.032))
        self.assertEqual(self.bot.account.trades[-1]["reason"], "SMART_RETREAT")
        await self.bot.manage_positions()
        self.assertIn(S, self.bot.positions)
        self.assertEqual(len(self.bot.account.trades), 2)

    async def test_trailing_both_sides_never_moves_back(self):
        for side, prices in [
            ("buy", [102, 103, 102.8, 102]),
            ("sell", [98, 97, 97.2, 98]),
        ]:
            with patch.object(
                phoenix.ccxt, "binanceusdm", return_value=self.data
            ), patch.object(phoenix.FinalPhoenix, "load_state"):
                self.bot = phoenix.FinalPhoenix()
            self.data.price = 100
            self.assertTrue(await self.bot.enter(S, side, 100, 0.1))
            stops = []
            for price in prices[:3]:
                self.data.price = price
                await self.bot.manage_positions()
                stops.append(self.bot.positions[S]["sl"])
            self.assertTrue(self.bot.positions[S]["trailing"])
            self.assertEqual(stops, sorted(stops, reverse=side == "sell"))
            self.data.price = prices[-1]
            await self.bot.manage_positions()
            self.assertNotIn(S, self.bot.positions)
            self.assertEqual(self.bot.account.trades[-1]["reason"], "TRAIL_SL")

    async def test_survival_enters_below_85_percent_and_exits_above_93(self):
        self.bot.account.balance = 84
        await self.bot.check_survival()
        self.assertTrue(self.bot.survival_mode)
        self.bot.account.balance = 93
        await self.bot.check_survival()
        self.assertTrue(self.bot.survival_mode)
        self.bot.account.balance = 94
        await self.bot.check_survival()
        self.assertFalse(self.bot.survival_mode)

    async def test_two_large_recorded_losses_arm_blacklist(self):
        for n in range(2):
            self.bot.last_trade.clear()
            await self.bot.enter(S, "buy", 100, 0.1)
            await self.bot.close_full(S, "SL", 94)
        self.assertTrue(self.bot.revenge_ready)
        self.assertEqual(self.bot.revenge_symbol, S)
        self.assertTrue(self.bot.is_blacklisted(S))

    async def test_state_keeps_150_executions_and_restores_saved_account(self):
        await self.bot.enter(S, "buy", 100, 0.1)
        self.bot.account.trades *= 160
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "sell"
        self.bot.survival_mode = True
        self.bot.recent_losses[S] = [-9, -10]
        self.bot.blacklist[S] = time.time() + 50
        self.bot.save_state()
        data = json.loads(phoenix.STATE_FILE.read_text())
        self.assertEqual(len(data["trades"]), 150)
        with patch.object(phoenix.ccxt, "binanceusdm", return_value=self.data):
            restored = phoenix.FinalPhoenix()
        self.assertEqual(restored.account.balance, self.bot.account.balance)
        self.assertEqual(restored.positions, self.bot.positions)
        for field in [
            "recent_losses",
            "revenge_ready",
            "revenge_symbol",
            "revenge_side",
            "survival_mode",
            "blacklist",
        ]:
            self.assertEqual(getattr(restored, field), getattr(self.bot, field))
        self.assertFalse(phoenix.STATE_FILE.with_suffix(".tmp").exists())

    async def test_margin_ramps_and_revenge_uses_65_percent_of_base(self):
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.12), 0.098)
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.185), 0.15)
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.5, True), 0.0585)
        self.bot.survival_mode = True
        self.assertAlmostEqual(self.bot.calc_margin_pct(0.5, True), 0.027)

    async def test_revenge_requires_opposite_signal_and_correct_quantity(self):
        await self.bot.enter(S, "buy", 100, 0.1)
        await self.bot.close_full(S, "SL", 94)
        self.assertEqual(self.bot.revenge_side, "sell")
        self.bot.last_trade.clear()
        expected = self.bot.account.balance * 0.09 * 0.65 * 50 / 100
        with patch.object(
            self.bot,
            "detect",
            AsyncMock(
                return_value={
                    "long": False,
                    "short": True,
                    "price": 100,
                    "strength": 0.3,
                }
            ),
        ):
            await self.bot.try_revenge()
        self.assertEqual(self.bot.positions[S]["side"], "sell")
        self.assertAlmostEqual(self.bot.positions[S]["amount"], expected, places=7)
        self.assertFalse(self.bot.revenge_ready)

    async def test_blacklisted_symbol_allows_revenge_but_not_regular_detection(self):
        self.bot.blacklist[S] = time.time() + 60
        self.assertIsNone(await self.bot.detect(S))
        self.assertTrue((await self.bot.detect(S, allow_blacklisted=True))["long"])
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "buy"
        await self.bot.try_revenge()
        self.assertTrue(self.bot.positions[S]["is_revenge"])
        self.assertFalse(self.bot.revenge_ready)
        self.assertIsNone(self.bot.revenge_symbol)
        self.assertIsNone(self.bot.revenge_side)
        saved = json.loads(phoenix.STATE_FILE.read_text())
        self.assertFalse(saved["revenge_ready"])
        self.assertTrue(self.bot.is_blacklisted(S))

    async def test_revenge_keeps_ready_for_no_signal_or_wrong_direction(self):
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "sell"
        for signal in [None, {"long": True, "short": False}]:
            with patch.object(self.bot, "detect", AsyncMock(return_value=signal)):
                await self.bot.try_revenge()
            self.assertTrue(self.bot.revenge_ready)
            self.assertEqual(self.bot.revenge_symbol, S)
            self.assertEqual(self.bot.revenge_side, "sell")
            self.assertFalse(self.bot.positions)

    async def test_failed_revenge_retries_after_cooldown(self):
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "buy"
        self.bot.last_trade[S] = time.time()
        await self.bot.try_revenge()
        self.assertTrue(self.bot.revenge_ready)
        self.assertFalse(self.bot.positions)
        self.bot.last_trade.clear()
        await self.bot.try_revenge()
        self.assertFalse(self.bot.revenge_ready)
        self.assertEqual(self.bot.positions[S]["side"], "buy")

    async def test_ticker_age_boundary_and_missing_timestamp(self):
        with patch.object(phoenix.time, "time", return_value=1000):
            for age in [0, 12]:
                ticker = {"last": 100, "timestamp": (1000 - age) * 1000}
                with patch.object(
                    self.data, "fetch_ticker", AsyncMock(return_value=ticker)
                ):
                    self.assertEqual(await self.bot.fresh_ticker(S), 100)
            with patch.object(
                self.data,
                "fetch_ticker",
                AsyncMock(return_value={"last": 100, "timestamp": 987999}),
            ):
                with self.assertRaisesRegex(ValueError, "Stale ticker"):
                    await self.bot.fresh_ticker(S)
            for ticker in [
                {"last": 100},
                {"last": 100, "timestamp": None},
                {"last": 100, "timestamp": 0},
            ]:
                with patch.object(
                    self.data, "fetch_ticker", AsyncMock(return_value=ticker)
                ):
                    self.assertEqual(await self.bot.fresh_ticker(S), 100)
        self.data.price = 0
        with self.assertRaises(ValueError):
            await self.bot.fresh_ticker(S)

    async def test_stale_ticker_blocks_entry_and_closing_until_retry(self):
        stale = {"last": 94, "timestamp": int((time.time() - 60) * 1000)}
        with patch.object(self.data, "fetch_ticker", AsyncMock(return_value=stale)):
            self.assertFalse(await self.bot.enter(S, "buy", 100, 0.1))
        self.assertFalse(self.bot.account.trades)
        self.assertTrue(await self.bot.enter(S, "buy", 1, 0.1))
        self.assertEqual(self.bot.positions[S]["entry"], 100)
        amount = self.bot.positions[S]["amount"]
        with patch.object(self.data, "fetch_ticker", AsyncMock(return_value=stale)):
            await self.bot.manage_positions()
            await self.bot.close_partial(S, 0.5, "SMART_RETREAT")
            await self.bot.close_full(S, "SL")
        self.assertEqual(self.bot.positions[S]["amount"], amount)
        self.assertEqual(len(self.bot.account.trades), 1)
        self.data.price = 94
        await self.bot.manage_positions()
        self.assertNotIn(S, self.bot.positions)

    async def test_shutdown_saves_state_and_closes_exchange(self):
        import asyncio

        started = asyncio.Event()

        async def wait_cycle():
            started.set()
            await asyncio.Event().wait()

        self.bot.scan_cycle = wait_cycle
        task = asyncio.create_task(self.bot.run())
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.data.closed)
        self.assertTrue(phoenix.STATE_FILE.exists())

    async def test_legacy_state_loads_with_new_defaults(self):
        phoenix.STATE_FILE.write_text(json.dumps({"balance": 87, "peak_balance": 100}))
        with patch.object(phoenix.ccxt, "binanceusdm", return_value=self.data):
            restored = phoenix.FinalPhoenix()
        self.assertEqual(restored.account.balance, 87)
        self.assertFalse(restored.revenge_ready)
        self.assertIsNone(restored.revenge_side)
        self.assertEqual(dict(restored.recent_losses), {})

    async def test_revenge_cannot_overwrite_existing_position(self):
        await self.bot.enter(S, "buy", 100, 0.1)
        self.bot.last_trade.clear()
        original = dict(self.bot.positions[S])
        balance = self.bot.account.balance
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "buy"
        await self.bot.try_revenge()
        self.assertTrue(self.bot.revenge_ready)
        self.assertEqual(self.bot.positions[S], original)
        self.assertEqual(self.bot.account.balance, balance)
        self.assertEqual(len(self.bot.account.trades), 1)
        with self.assertRaisesRegex(ValueError, "already exists"):
            await self.bot.account.create_order(S, "sell", 1)
        self.assertEqual(self.bot.account.balance, balance)

    async def test_overclose_fees_use_actual_quantity(self):
        account = self.bot.account
        account.prices[S] = 100
        await account.create_order(S, "buy", 1)
        account.prices[S] = 110
        trade = await account.create_order(S, "sell", 5, reduce_only=True)
        self.assertEqual(trade["amount"], 1)
        self.assertAlmostEqual(trade["fee_usdt"], 0.055)
        self.assertAlmostEqual(account.balance, 100 - 0.05 + 10 - 0.055)
        self.assertFalse(account.positions)

    async def test_invalid_order_values_do_not_mutate_account(self):
        account = self.bot.account
        account.prices[S] = 100
        for amount in [0, -1, float("nan"), float("inf")]:
            with self.assertRaises(ValueError):
                await account.create_order(S, "buy", amount)
        self.assertEqual(account.balance, 100)
        self.assertFalse(account.positions)
        self.assertFalse(account.trades)
        await account.create_order(S, "buy", 1)
        with self.assertRaises(ValueError):
            await account.create_order(S, "buy", 1, reduce_only=True)
        self.assertEqual(account.positions[S]["amount"], 1)

    async def test_invalid_ticker_and_precision_failure_keep_revenge_ready(self):
        for price in [float("nan"), float("inf"), -1]:
            self.data.price = price
            with self.assertRaises(ValueError):
                await self.bot.fresh_ticker(S)
        self.data.price = 100
        self.bot.revenge_ready = True
        self.bot.revenge_symbol = S
        self.bot.revenge_side = "buy"
        with patch.object(
            self.data,
            "amount_to_precision",
            side_effect=ValueError("precision unavailable"),
        ):
            await self.bot.try_revenge()
        self.assertTrue(self.bot.revenge_ready)
        self.assertFalse(self.bot.positions)

    async def test_shutdown_closes_exchange_even_if_save_fails(self):
        import asyncio

        started = asyncio.Event()

        async def wait_cycle():
            started.set()
            await asyncio.Event().wait()

        self.bot.scan_cycle = wait_cycle
        with patch.object(self.bot, "save_state", side_effect=OSError("disk full")):
            task = asyncio.create_task(self.bot.run())
            await started.wait()
            task.cancel()
            with self.assertRaisesRegex(OSError, "disk full"):
                await task
        self.assertTrue(self.data.closed)

    async def test_corrupt_state_is_preserved_and_startup_stops(self):
        phoenix.STATE_FILE.write_text("{broken")
        with patch.object(phoenix.ccxt, "binanceusdm") as factory:
            with self.assertRaisesRegex(RuntimeError, "original file preserved"):
                phoenix.FinalPhoenix()
            factory.assert_not_called()
        self.assertEqual(phoenix.STATE_FILE.read_text(), "{broken")

    async def test_partial_close_keeps_account_and_strategy_quantities_identical(self):
        await self.bot.enter(S, "buy", 100, 0.1)
        await self.bot.close_partial(S, 0.5, "SMART_RETREAT")
        self.assertEqual(
            self.bot.positions[S]["amount"], self.bot.account.positions[S]["amount"]
        )
        await self.bot.close_partial(S, 1, "TEST_CLOSE")
        self.assertNotIn(S, self.bot.positions)
        self.assertNotIn(S, self.bot.account.positions)

    async def test_watch_list_refreshes_at_600_seconds_not_every_pass(self):
        top = AsyncMock(side_effect=[[S], ["ETH/USDT:USDT"]])
        with patch.object(self.bot, "get_top_symbols", top), patch.object(
            phoenix.time, "monotonic", return_value=100
        ) as clock:
            self.assertEqual(await self.bot.get_watch_symbols(), (S,))
            clock.return_value = 699
            self.assertEqual(await self.bot.get_watch_symbols(), (S,))
            self.assertEqual(top.await_count, 1)
            clock.return_value = 700
            self.assertEqual(await self.bot.get_watch_symbols(), ("ETH/USDT:USDT",))
            self.assertEqual(top.await_count, 2)

    async def test_empty_universe_is_cached_until_refresh(self):
        with patch.object(
            self.bot, "get_top_symbols", AsyncMock(return_value=[])
        ) as top:
            await self.bot.get_watch_symbols()
            await self.bot.get_watch_symbols()
            self.assertEqual(top.await_count, 1)

    async def test_new_signal_on_next_pass_enters_without_universe_refresh(self):
        no_signal = {
            "symbol": S,
            "long": False,
            "short": False,
            "strength": 0.1,
            "price": 100,
        }
        signal = dict(no_signal, long=True)
        with patch.object(
            self.bot, "get_top_symbols", AsyncMock(return_value=[S])
        ) as top:
            with patch.object(
                self.bot, "detect", AsyncMock(side_effect=[no_signal, signal])
            ) as detect:
                await self.bot.scan_cycle()
                self.assertFalse(self.bot.positions)
                await self.bot.scan_cycle()
                self.assertIn(S, self.bot.positions)
                self.assertEqual(top.await_count, 1)
                self.assertEqual(detect.await_count, 2)

    async def test_all_40_symbols_checked_with_bounded_concurrency_and_live_exit_watcher(
        self,
    ):
        import asyncio

        symbols = [f"SYM{n}/USDT:USDT" for n in range(40)]
        active = 0
        peak = 0
        four_started = asyncio.Event()
        release = asyncio.Event()
        exit_checked = asyncio.Event()

        async def detect(symbol):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == phoenix.SCAN_CONCURRENCY:
                four_started.set()
            try:
                await release.wait()
                return None
            finally:
                active -= 1

        async def manage():
            exit_checked.set()

        with patch.object(
            self.bot, "get_watch_symbols", AsyncMock(return_value=symbols)
        ):
            with patch.object(
                self.bot, "detect", AsyncMock(side_effect=detect)
            ) as checks:
                with patch.object(self.bot, "manage_positions", manage):
                    scan = asyncio.create_task(self.bot.scan_cycle())
                    watcher = None
                    try:
                        await asyncio.wait_for(four_started.wait(), 1)
                        watcher = asyncio.create_task(self.bot.exit_watcher())
                        await asyncio.wait_for(exit_checked.wait(), 1)
                        self.assertFalse(scan.done())
                        release.set()
                        await scan
                    finally:
                        release.set()
                        scan.cancel()
                        if watcher:
                            watcher.cancel()
                        await asyncio.gather(
                            scan,
                            *([watcher] if watcher else []),
                            return_exceptions=True,
                        )
                self.assertEqual(checks.await_count, 40)
                self.assertEqual(peak, phoenix.SCAN_CONCURRENCY)

    async def test_run_repeats_scan_with_two_second_pause_not_600(self):
        import asyncio

        count = 0

        async def cycle():
            nonlocal count
            count += 1
            if count == 2:
                raise asyncio.CancelledError()

        async def background():
            await asyncio.Event().wait()

        with patch.object(self.bot, "scan_cycle", side_effect=cycle):
            with patch.object(self.bot, "exit_watcher", background), patch.object(
                self.bot, "status_loop", background
            ):
                with patch.object(phoenix.asyncio, "sleep", new=AsyncMock()) as sleep:
                    with self.assertRaises(asyncio.CancelledError):
                        await self.bot.run()
        sleep.assert_awaited_once_with(phoenix.ENTRY_SCAN_PAUSE)
        self.assertEqual(count, 2)
        self.assertEqual(phoenix.ENTRY_SCAN_PAUSE, 2)
        self.assertEqual(phoenix.SCAN_INTERVAL, 600)


if __name__ == "__main__":
    unittest.main()
