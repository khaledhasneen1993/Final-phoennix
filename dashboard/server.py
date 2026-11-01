"""Loopback dashboard and supervisor for three isolated paper workers."""

import argparse
import asyncio
import csv
import fcntl
import io
import json
import os
import secrets
import signal
import sqlite3
import sys
import subprocess
import time
from collections import deque
from pathlib import Path
from aiohttp import web
from ledger import round_trips, read_archive, fill_key

from paths import ASSETS, RUNTIME, SOURCES, NAMES, LABELS, STATE_PATHS, archive_path


class Supervisor:
    def __init__(self):
        self.procs = {}
        self.locks = {n: asyncio.Lock() for n in NAMES}
        self.logs = {n: deque(maxlen=120) for n in NAMES}
        self.readers = {}
        self.started = {}
        self.token = secrets.token_urlsafe(32)
        self.sources = {}
        for name, path in SOURCES.items():
            try:
                commit = subprocess.check_output(
                    ["git", "-C", str(path), "rev-parse", "--short", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except (OSError, subprocess.CalledProcessError):
                commit = "unknown"
            self.sources[name] = {"repository": LABELS[name], "commit": commit}
        (RUNTIME / "data").mkdir(parents=True, exist_ok=True)
        (RUNTIME / "logs").mkdir(exist_ok=True)
        self.db = sqlite3.connect(RUNTIME / "data/history.sqlite")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS equity(bot TEXT, time REAL, value REAL)"
        )
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS equity_bot_time ON equity(bot,time)"
        )
        self.db.commit()
        for name in NAMES:
            path = RUNTIME / "logs" / f"{name}.log"
            if path.is_file():
                with path.open("rb") as f:
                    f.seek(max(0, path.stat().st_size - 65536))
                    lines = f.read().decode(errors="replace").splitlines()
                self.logs[name].extend(lines[-120:])

    async def start(self, name):
        async with self.locks[name]:
            p = self.procs.get(name)
            if p and p.returncode is None:
                return
            env = dict(os.environ)
            env.pop("CASCADE_DASHBOARD_TOKEN", None)
            env["CASCADE_RUNTIME_DIR"] = str(RUNTIME)
            env["CASCADE_FINAL_DIR"] = str(SOURCES["final"])
            env["CASCADE_FINAL3_DIR"] = str(SOURCES["final3"])
            for key, bot in [
                ("CASCADE_FINAL_STATE", "final"),
                ("CASCADE_FINAL3_STATE", "final3"),
                ("CASCADE_PHOENIX_STATE", "phoenix"),
            ]:
                env[key] = str(STATE_PATHS[bot])
            self.started[name] = time.time()
            self.procs[name] = await asyncio.create_subprocess_exec(
                sys.executable,
                "-u",
                str(ASSETS / "worker.py"),
                name,
                cwd=ASSETS,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,
            )
            self.readers[name] = asyncio.create_task(
                self.read_logs(name, self.procs[name])
            )

    async def read_logs(self, name, proc):
        # Rotate on worker starts; bound active logs while retaining recent output.
        path = RUNTIME / "logs" / f"{name}.log"

        def rotate():
            if path.exists() and path.stat().st_size > 5_000_000:
                path.replace(path.with_suffix(".previous.log"))

        while line := await proc.stdout.readline():
            text = line.decode(errors="replace").rstrip()
            self.logs[name].append(text)
            rotate()
            with path.open("a") as f:
                f.write(text + "\n")
        await proc.wait()

    async def stop(self, name):
        async with self.locks[name]:
            p = self.procs.get(name)
            if p and p.returncode is None:
                p.send_signal(signal.SIGINT)
                try:
                    await asyncio.wait_for(p.wait(), 40)
                except asyncio.TimeoutError:
                    p.kill()
                    await p.wait()
                    self.logs[name].append(
                        "Forced stop after timeout; check saved state before resuming."
                    )
            reader = self.readers.get(name)
            if reader:
                await reader

    def snapshot(self, name):
        p = self.procs.get(name)
        running = bool(p and p.returncode is None)
        try:
            s = json.loads((RUNTIME / "data" / f"{name}.snapshot.json").read_text())
        except (OSError, ValueError):
            s = {}
        updated = s.get("updated_at", 0)
        age = time.time() - updated if updated else None
        fresh_run = updated >= self.started.get(name, 0)
        live = running and fresh_run and age is not None and age < 20
        sc = s.get("scanner", {})
        market_age = sc.get("last_event_age")
        s.update(
            process_running=running,
            snapshot_age=age,
            live=bool(
                live
                and sc.get("connected")
                and market_age is not None
                and market_age + age < 10
            ),
            process_status=(
                "starting"
                if running and (not fresh_run or s.get("status") == "starting")
                else (
                    "running"
                    if live
                    else (
                        "stale"
                        if running
                        else (
                            "error"
                            if p and p.returncode not in (0, None)
                            else "stopped"
                        )
                    )
                )
            ),
            exit_code=p.returncode if p else None,
            logs=list(self.logs[name]),
        )
        s["source"] = self.sources[name]
        s["state_path"] = str(STATE_PATHS[name])
        s["archive_path"] = str(archive_path(name))
        s["process_healthy"] = bool(live)
        if sc.get("transport") == "rest":
            s["live"] = bool(
                live and market_age is not None and market_age + (age or 0) <= 12
            )
        try:
            archived = read_archive(archive_path(name))
            # Current snapshot can contain enriched fields from a just-completed entry.
            merged = {fill_key(f): f for f in archived}
            merged.update({fill_key(f): f for f in s.get("trades", [])})
            s["trades"] = list(merged.values())
        except (OSError, ValueError, sqlite3.Error) as exc:
            s["archive_error"] = str(exc)
        for fill in s.get("trades", []):
            fill["dashboard_fill_id"] = fill_key(fill)
        s["archive_note"] = (
            "All available imported and new fills; history missing before import cannot be recovered."
        )
        health = []
        for item in sc.get("symbol_health", []):
            item = dict(item)
            if item.get("age") is not None:
                item["age"] += max(0, age or 0)
            if item.get("event_age") is not None:
                item["event_age"] += max(0, age or 0)
            threshold = sc.get("freshness_limit", 10)
            connected = sc.get("transport") == "rest" or sc.get("connected")
            item["fresh"] = bool(
                live
                and connected
                and item.get("age") is not None
                and item["age"] <= threshold
                and item.get("event_age") is not None
                and -threshold <= item["event_age"] <= threshold
            )
            health.append(item)
        sc["symbol_health"] = health
        sc["fresh_symbols"] = sum(x["fresh"] for x in health)
        s["scanner"] = sc
        s["open_trades"], s["closed_trades"], s["unmatched_exits"] = round_trips(
            s.get("trades", [])
        )
        closed = s["closed_trades"]
        complete = [x for x in closed if x["history_complete"]]
        s["stats"] = {
            "closed": len(complete),
            "wins": sum(x["net_pnl_usdt"] > 0 for x in complete),
            "incomplete": len(closed) - len(complete),
            "unmatched": len(s["unmatched_exits"]),
            "realized_net": sum(x["net_pnl_usdt"] for x in complete),
            "fees": sum(x.get("fee_usdt", 0) for x in s.get("trades", [])),
        }
        s["history"] = list(
            reversed(
                self.db.execute(
                    "SELECT time,value FROM equity WHERE bot=? ORDER BY time DESC LIMIT 600",
                    (name,),
                ).fetchall()
            )
        )
        return s

    async def history(self):
        while True:
            for name in NAMES:
                s = self.snapshot(name)
                if s.get("process_healthy") and "equity_usdt" in s:
                    self.db.execute(
                        "INSERT INTO equity VALUES(?,?,?)",
                        (name, time.time(), s["equity_usdt"]),
                    )
            self.db.execute(
                "DELETE FROM equity WHERE time < ?", (time.time() - 30 * 86400,)
            )
            self.db.commit()
            await asyncio.sleep(30)


def make_app(supervisor, autostart=True):
    @web.middleware
    async def protect(request, handler):
        if request.host not in ("127.0.0.1:8787", "localhost:8787"):
            raise web.HTTPForbidden(text="Loopback host required")
        if request.method == "POST":
            if not secrets.compare_digest(
                request.headers.get("X-Control-Token", ""), supervisor.token
            ):
                raise web.HTTPForbidden()
            if request.headers.get("Origin") not in (
                None,
                "http://127.0.0.1:8787",
                "http://localhost:8787",
            ):
                raise web.HTTPForbidden()
        response = await handler(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            }
        )
        return response

    app = web.Application(middlewares=[protect])

    async def index(request):
        return web.FileResponse(ASSETS / "index.html")

    async def state(request):
        return web.json_response(
            {
                "token": supervisor.token,
                "now": time.time(),
                "bots": {n: supervisor.snapshot(n) for n in NAMES},
            }
        )

    async def control(request):
        name, action = request.match_info["name"], request.match_info["action"]
        if name not in NAMES or action not in ("start", "stop"):
            raise web.HTTPNotFound()
        await getattr(supervisor, action)(name)
        return web.json_response({"ok": True})

    async def export(request):
        name, kind = request.match_info["name"], request.match_info["kind"]
        if name not in (*NAMES, "all") or kind not in ("fills", "closed", "snapshot"):
            raise web.HTTPNotFound()
        s = (
            supervisor.snapshot(name)
            if name != "all"
            else {"bots": {n: supervisor.snapshot(n) for n in NAMES}}
        )
        if kind == "snapshot":
            return web.json_response(
                s,
                headers={
                    "Content-Disposition": f'attachment; filename="{name}-snapshot.json"'
                },
            )
        key = "trades" if kind == "fills" else "closed_trades"
        rows = (
            s.get(key, [])
            if name != "all"
            else [
                dict(row, bot=LABELS[n])
                for n, b in s["bots"].items()
                for row in b.get(key, [])
            ]
        )
        keys = sorted({k for row in rows for k in row})
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=keys)
        writer.writeheader()
        for row in rows:
            clean = {
                k: (
                    json.dumps(v, ensure_ascii=False)
                    if isinstance(v, (list, dict))
                    else v
                )
                for k, v in row.items()
            }
            # Prevent spreadsheet formulas if a future imported string starts with one.
            clean = {
                k: (
                    "'" + v
                    if isinstance(v, str) and v.startswith(("=", "+", "-", "@"))
                    else v
                )
                for k, v in clean.items()
            }
            writer.writerow(clean)
        return web.Response(
            body=("\ufeff" + output.getvalue()).encode(),
            content_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{name}-{kind}.csv"'
            },
        )

    app.router.add_get("/", index)

    async def javascript(request):
        return web.FileResponse(ASSETS / "app.js")

    app.router.add_get("/app.js", javascript)

    async def stylesheet(request):
        return web.FileResponse(ASSETS / "style.css")

    app.router.add_get("/style.css", stylesheet)
    app.router.add_get("/api/status", state)
    app.router.add_post("/api/{name}/{action}", control)
    app.router.add_get("/export/{name}/{kind}", export)

    async def lifecycle(app):
        sampler = None
        try:
            if autostart:
                for n in NAMES:
                    await supervisor.start(n)
            sampler = asyncio.create_task(supervisor.history())
            yield
        finally:
            if sampler:
                sampler.cancel()
                await asyncio.gather(sampler, return_exceptions=True)
            await asyncio.gather(*(supervisor.stop(n) for n in NAMES))
            supervisor.db.close()

    app.cleanup_ctx.append(lifecycle)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args()
    (RUNTIME / "data").mkdir(parents=True, exist_ok=True)
    lock = (RUNTIME / "data/server.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Dashboard is already running. Open http://127.0.0.1:8787")
    web.run_app(
        make_app(Supervisor(), not args.no_autostart),
        host="127.0.0.1",
        port=8787,
        shutdown_timeout=50,
        access_log=None,
    )
