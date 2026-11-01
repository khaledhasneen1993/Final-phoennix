import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from ledger import FillArchive, read_archive, round_trips


def fills():
    def f(ts, side, amount, price, **extra):
        return dict(
            timestamp=f"2026-10-31T12:00:{ts:02d}+00:00",
            symbol="BTC",
            side=side,
            amount=amount,
            price=price,
            fee_usdt=amount * price * 0.0005,
            **extra,
        )

    return [
        f(1, "buy", 5, 100, reduceOnly=False),
        f(2, "buy", 5, 97.5, reduceOnly=False, scaleIn=True),
        f(
            3,
            "sell",
            5,
            95,
            reduceOnly=True,
            partial_close=True,
            pnl_usdt=-18.75,
            exit_reason="PARTIAL_RISK",
        ),
        f(
            4,
            "sell",
            5,
            100,
            reduceOnly=True,
            partial_close=False,
            pnl_usdt=6.25,
            exit_reason="BE_SL",
        ),
    ]


class LedgerTests(unittest.TestCase):
    def test_partial_keeps_position_open_and_sums_fees(self):
        rows = fills()
        opened, closed, unmatched = round_trips(rows[:3])
        self.assertEqual(len(opened), 1)
        self.assertFalse(closed)
        self.assertFalse(unmatched)
        self.assertEqual(opened[0]["remaining_amount"], 5)
        self.assertEqual(opened[0]["average_entry"], 98.75)
        opened, closed, unmatched = round_trips(rows)
        self.assertFalse(opened)
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["partials"], 1)
        self.assertEqual(closed[0]["adds"], 1)
        self.assertAlmostEqual(
            closed[0]["net_pnl_usdt"], -12.5 - sum(r["fee_usdt"] for r in rows)
        )

    def test_truncated_history_is_not_invented(self):
        opened, closed, unmatched = round_trips(fills()[2:])
        self.assertFalse(opened)
        self.assertFalse(closed)
        self.assertEqual(len(unmatched), 2)
        opened, closed, unmatched = round_trips(fills()[1:])
        self.assertFalse(closed[0]["history_complete"])

    def test_archive_deduplicates_and_survives_150_item_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fills.sqlite"
            db = FillArchive(path)
            rows = [
                dict(
                    fills()[0], timestamp=f"2026-10-31T12:{n//60:02d}:{n%60:02d}+00:00"
                )
                for n in range(170)
            ]
            db.sync(rows)
            db.sync(rows[-150:])
            self.assertEqual(len(db.read()), 170)
            updated = dict(rows[0], strength=0.2)
            db.sync([updated])
            self.assertEqual(db.read()[0]["strength"], 0.2)
            db.close()
            self.assertEqual(len(read_archive(path)), 170)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patches = [
            patch.object(server, "RUNTIME", self.root),
            patch.object(
                server,
                "STATE_PATHS",
                {n: self.root / f"{n}.json" for n in server.NAMES},
            ),
            patch.object(server, "archive_path", lambda n: self.root / f"{n}.sqlite"),
        ]
        for p in self.patches:
            p.start()
        self.supervisor = server.Supervisor()
        self.client = TestClient(
            TestServer(server.make_app(self.supervisor, autostart=False))
        )
        await self.client.start_server()
        self.host = {"Host": "127.0.0.1:8787"}

    async def asyncTearDown(self):
        self.supervisor.procs.clear()
        await self.client.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def snapshot(self, name, rows=None, **extra):
        state = dict(
            updated_at=time.time(),
            balance_usdt=100,
            equity_usdt=100,
            trades=rows or [],
            account_positions={},
            strategy_positions={},
            **extra,
        )
        (self.root / "data" / f"{name}.snapshot.json").write_text(json.dumps(state))

    async def test_all_three_and_exports_include_partials(self):
        for name in server.NAMES:
            self.snapshot(name, fills())
        response = await self.client.get("/api/status", headers=self.host)
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(set(payload["bots"]), {"final", "final3", "phoenix"})
        self.assertEqual(payload["bots"]["phoenix"]["stats"]["closed"], 1)
        for kind in ["snapshot", "fills", "closed"]:
            response = await self.client.get("/export/all/" + kind, headers=self.host)
            self.assertEqual(response.status, 200)
            text = await response.text()
            self.assertIn("Phoenix", text)
            if kind != "closed":
                self.assertIn("PARTIAL_RISK", text)
        response = await self.client.get("/export/final2/snapshot", headers=self.host)
        self.assertEqual(response.status, 404)

    async def test_archive_merges_metadata_with_new_window(self):
        rows = fills()
        archive = FillArchive(self.root / "phoenix.sqlite")
        archive.sync(rows)
        archive.close()
        self.snapshot("phoenix", [dict(rows[-1], extra="new")])
        s = self.supervisor.snapshot("phoenix")
        self.assertEqual(len(s["trades"]), 4)
        self.assertEqual(s["trades"][-1]["extra"], "new")
        self.assertEqual(s["stats"]["closed"], 1)
        self.assertTrue(s["trades"][0]["dashboard_fill_id"])

    async def test_scan_diagnostics_are_preserved_in_status(self):
        report = dict(
            phase="waiting",
            checked=40,
            successful=39,
            failed=1,
            signals=0,
            entry_results={},
            checks={
                "BTC": {
                    "status": "error",
                    "errors": [{"stage": "order_book", "message": "timeout"}],
                }
            },
        )
        self.snapshot("phoenix", scan_report=report)
        response = await self.client.get("/api/status", headers=self.host)
        payload = await response.json()
        self.assertEqual(payload["bots"]["phoenix"]["scan_report"], report)

    async def test_control_origin_host_and_token(self):
        response = await self.client.post("/api/phoenix/stop", headers=self.host)
        self.assertEqual(response.status, 403)
        headers = {
            **self.host,
            "X-Control-Token": self.supervisor.token,
            "Origin": "https://other.example",
        }
        response = await self.client.post("/api/phoenix/stop", headers=headers)
        self.assertEqual(response.status, 403)
        headers["Origin"] = "http://127.0.0.1:8787"
        response = await self.client.post("/api/phoenix/stop", headers=headers)
        self.assertEqual(response.status, 200)
        response = await self.client.get(
            "/api/status", headers={"Host": "other.example"}
        )
        self.assertEqual(response.status, 403)

    async def test_assets_and_mobile_selector(self):
        for path in ["/", "/app.js", "/style.css"]:
            response = await self.client.get(path, headers=self.host)
            self.assertEqual(response.status, 200)
            if path == "/":
                self.assertIn('data-bot="phoenix"', await response.text())

    async def test_rest_health_uses_12_second_limit(self):
        class Proc:
            returncode = None

        self.supervisor.procs["phoenix"] = Proc()
        self.supervisor.started["phoenix"] = time.time() - 30
        self.snapshot(
            "phoenix",
            scanner=dict(
                transport="rest",
                connected=None,
                symbols=["BTC"],
                last_event_age=11,
                freshness_limit=12,
                symbol_health=[dict(symbol="BTC", age=11, event_age=11)],
            ),
        )
        s = self.supervisor.snapshot("phoenix")
        self.assertTrue(s["live"])
        self.assertEqual(s["scanner"]["fresh_symbols"], 1)
        self.snapshot(
            "phoenix",
            scanner=dict(
                transport="rest",
                connected=None,
                symbols=["BTC"],
                last_event_age=13,
                freshness_limit=12,
                symbol_health=[dict(symbol="BTC", age=13, event_age=13)],
            ),
        )
        s = self.supervisor.snapshot("phoenix")
        self.assertFalse(s["live"])
        self.assertEqual(s["scanner"]["fresh_symbols"], 0)

    async def test_duplicate_start_and_environment_isolation(self):
        class Process:
            returncode = None

            def send_signal(self, sig):
                self.returncode = 0

            async def wait(self):
                return self.returncode

        async def no_logs(*args):
            pass

        with patch(
            "asyncio.create_subprocess_exec", return_value=Process()
        ) as spawn, patch.object(self.supervisor, "read_logs", no_logs):
            await self.supervisor.start("phoenix")
            await self.supervisor.start("phoenix")
            self.assertEqual(spawn.call_count, 1)
            env = spawn.call_args.kwargs["env"]
            self.assertNotIn("CASCADE_DASHBOARD_TOKEN", env)
            self.assertEqual(
                env["CASCADE_PHOENIX_STATE"], str(server.STATE_PATHS["phoenix"])
            )
            await self.supervisor.stop("phoenix")
            self.assertEqual(self.supervisor.procs["phoenix"].returncode, 0)

    async def test_incomplete_history_excluded_from_statistics(self):
        self.snapshot("phoenix", fills()[1:])
        s = self.supervisor.snapshot("phoenix")
        self.assertEqual(s["stats"]["closed"], 0)
        self.assertEqual(s["stats"]["incomplete"], 1)


if __name__ == "__main__":
    unittest.main()
