"""Wrap each upstream bot in a separate process; strategy source remains unchanged."""

import asyncio
import fcntl
import json
import logging
import signal
import sys
import time
from paths import NAMES, LABELS, RUNTIME, SOURCES, STATE_PATHS, archive_path
from ledger import FillArchive

log = logging.getLogger("DASHBOARD")


def phoenix_settings(module):
    settings = {
        key: getattr(module, key)
        for key in (
            "LEVERAGE",
            "CASCADE_THRESHOLD",
            "VOLUME_SPIKE",
            "TAKE_PROFIT_PCT",
            "STOP_LOSS_PCT",
            "COOLDOWN_SEC",
            "SCAN_INTERVAL",
            "MIN_QUOTE_VOLUME",
            "RETREAT_LEVEL",
            "SURVIVAL_THRESHOLD",
            "REVENGE_MIN_LOSS",
            "REVENGE_SIZE_MULT",
            "BLACKLIST_MINUTES",
        )
    }
    settings.update(
        mode="phoenix",
        MAX_POSITION_PCT=module.BASE_MARGIN_PCT,
        MAX_MARGIN_PCT=module.MAX_MARGIN_PCT,
        MAX_CONCURRENT_POSITIONS=module.MAX_POSITIONS,
        MAX_SYMBOLS_TO_SCAN=module.MAX_SYMBOLS,
        add_levels=[],
        trail_activation=module.TAKE_PROFIT_PCT,
        trail_distance=module.TRAIL_DISTANCE,
        retreat_stop_distance=0.032,
        partial_fraction=0.5,
        max_price_age=module.MAX_TICKER_AGE,
        watch_mode="rest_scan",
        exit_interval=7,
        size_formula="min(0.15, 0.09 + max(0, strength - 0.11) * 0.8)",
    )
    return settings


def snapshot_state(name, bot, settings, status, ticker_health):
    if name == "phoenix":
        account = bot.account
        state = dict(
            initial_balance_usdt=100.0,
            balance_usdt=account.balance,
            equity_usdt=account.equity,
            free_usdt=account.free,
            used_usdt=account.used,
            account_positions=account.positions,
            strategy_positions=bot.positions,
            prices=account.prices,
            trades=account.trades[-150:],
            settings=settings,
            fee_rate=account.fee_rate,
            last_trade=bot.last_trade,
        )
        state["scan_report"] = bot.dashboard_scan
        state["risk"] = dict(
            peak_balance=account.peak_balance,
            survival_mode=bot.survival_mode,
            revenge_ready=bot.revenge_ready,
            revenge_symbol=bot.revenge_symbol,
            revenge_side=bot.revenge_side,
            blacklist=bot.blacklist,
        )
        now = time.time()
        health = []
        for symbol, item in ticker_health.items():
            age = now - item["received"]
            event_age = now - item["event"] if item.get("event") is not None else None
            health.append(
                dict(
                    symbol=symbol,
                    age=age,
                    event_age=event_age,
                    fresh=bool(age <= 12 and event_age is not None and event_age <= 12),
                    timestamp_available=event_age is not None,
                    error=item.get("error"),
                )
            )
        state["scanner"] = dict(
            transport="rest",
            connected=None,
            symbols=list(ticker_health),
            events=bot.dashboard_tickers,
            entries=bot.dashboard_entries,
            book_checks=bot.dashboard_book_checks,
            last_event_age=max(
                (
                    h["event_age"]
                    for h in health
                    if h["symbol"] in account.positions and h["event_age"] is not None
                ),
                default=None,
            ),
            fresh_symbols=sum(h["fresh"] for h in health),
            symbol_health=health,
            freshness_limit=12,
        )
    else:
        account = bot.exchange
        state = dict(
            initial_balance_usdt=100.0,
            balance_usdt=account.balance,
            equity_usdt=account.equity,
            free_usdt=account.free,
            used_usdt=account.used,
            account_positions=account.positions,
            strategy_positions=bot.positions,
            prices=account.prices,
            trades=account.trades,
            settings=bot.settings(),
            fee_rate=account.fee_rate,
            last_trade=bot.last_trade,
        )
        sc = bot.scanner
        mono, wall = sc.clock(), sc.wall_clock()
        health = []
        for symbol in sc.symbols:
            received, event = sc.received.get(symbol), sc.event_times.get(symbol)
            age = mono - received if received is not None else None
            event_age = wall - event / 1000 if event is not None else None
            health.append(
                dict(
                    symbol=symbol,
                    age=age,
                    event_age=event_age,
                    fresh=bool(
                        sc.connected
                        and age is not None
                        and age <= 10
                        and event_age is not None
                        and -10 <= event_age <= 10
                    ),
                    seeded=symbol in sc.histories,
                )
            )
        state["scanner"] = dict(
            transport="websocket",
            connected=sc.connected,
            symbols=list(sc.symbols),
            events=sc.events,
            entries=sc.entries,
            book_checks=sc.book_checks,
            last_event_age=min(
                (h["age"] for h in health if h["age"] is not None), default=None
            ),
            fresh_symbols=sum(h["fresh"] for h in health),
            symbol_health=health,
            freshness_limit=10,
        )
    state.update(
        bot=name,
        status=status,
        updated_at=time.time(),
        state_path=str(STATE_PATHS[name]),
    )
    return state


def observe_phoenix(bot, module, health):
    """Observe upstream methods without changing strategy decisions."""
    bot.dashboard_scan = dict(
        phase="starting",
        checked=0,
        total=0,
        successful=0,
        failed=0,
        signals=0,
        entries=0,
        move_pass=0,
        volume_pass=0,
        book_pass=0,
        checks={},
        entry_results={},
    )
    bot.dashboard_tickers = bot.dashboard_entries = bot.dashboard_book_checks = 0
    original_fetch = bot.exchange.fetch_ticker

    async def fetch(symbol, *args, **kwargs):
        try:
            result = await original_fetch(symbol, *args, **kwargs)
            ts = result.get("timestamp")
            health[symbol] = dict(
                received=time.time(), event=float(ts) / 1000 if ts else None
            )
            bot.dashboard_tickers += 1
            return result
        except Exception as exc:
            if symbol in health:
                health[symbol]["error"] = str(exc)
            raise

    bot.exchange.fetch_ticker = fetch
    original_top = bot.get_top_symbols

    async def top():
        symbols = await original_top()
        bot.dashboard_scan["total"] = len(symbols)
        return symbols

    bot.get_top_symbols = top
    original_book = bot.get_imbalance

    async def book(symbol):
        bot.dashboard_book_checks += 1
        return await original_book(symbol)

    bot.get_imbalance = book
    original_detect = bot.detect

    async def detect(symbol, allow_blacklisted=False):
        sig = await original_detect(symbol, allow_blacklisted=allow_blacklisted)
        # Revenge checks are separate from the universe scan counters.
        if allow_blacklisted:
            return sig
        report = bot.dashboard_scan
        row = dict(
            symbol=symbol, checked_at=time.time(), status="error", signal="", errors=[]
        )
        if sig is None:
            if bot.is_blacklisted(symbol):
                row.update(status="skipped", signal="محظورة مؤقتًا")
            else:
                row["errors"] = [
                    dict(
                        stage="detect",
                        message="تعذر حساب الإشارة؛ راجع الاتصال والشموع",
                    )
                ]
                report["failed"] += 1
        else:
            row.update(
                status="ok",
                ret=float(sig["ret"]),
                vol_spike=float(sig["vol_spike"]),
                imb=float(sig["imb"]),
                strength=float(sig["strength"]),
                move_pass=bool(abs(sig["ret"]) > module.CASCADE_THRESHOLD),
                volume_pass=bool(sig["vol_spike"] > module.VOLUME_SPIKE),
                book_pass=bool(
                    (sig["ret"] > 0 and sig["imb"] > 0.35)
                    or (sig["ret"] < 0 and sig["imb"] < -0.35)
                ),
                signal="LONG" if sig["long"] else "SHORT" if sig["short"] else "",
            )
            report["successful"] += 1
            report["signals"] += int(sig["long"] or sig["short"])
            for key in ("move_pass", "volume_pass", "book_pass"):
                report[key] += int(row[key])
        report["checks"][symbol] = row
        report["checked"] += 1
        return sig

    bot.detect = detect
    original_enter = bot.enter

    async def enter(symbol, *args, **kwargs):
        success = await original_enter(symbol, *args, **kwargs)
        bot.dashboard_scan["entry_results"][symbol] = dict(
            success=success, at=time.time(), reason=kwargs.get("reason", "CASCADE")
        )
        if success:
            if bot.account.trades:
                bot.account.trades[-1].update(
                    strength=bot.positions[symbol]["strength"],
                    margin_pct=bot.positions[symbol]["margin_pct"],
                )
            bot.dashboard_scan["entries"] += 1
            bot.dashboard_entries += 1
        return success

    bot.enter = enter
    original_cycle = bot.scan_cycle

    async def cycle():
        bot.dashboard_scan = dict(
            phase="scanning",
            started_at=time.time(),
            checked=0,
            total=0,
            successful=0,
            failed=0,
            signals=0,
            entries=0,
            move_pass=0,
            volume_pass=0,
            book_pass=0,
            checks={},
            entry_results={},
        )
        try:
            result = await original_cycle()
            bot.dashboard_scan["phase"] = "waiting"
            return result
        except Exception as exc:
            bot.dashboard_scan.update(phase="error", cycle_error=str(exc))
            raise
        finally:
            bot.dashboard_scan.update(
                finished_at=time.time(), next_scan_at=time.time() + module.SCAN_INTERVAL
            )

    bot.scan_cycle = cycle


async def run_worker(name):
    sys.path.insert(0, str(SOURCES[name]))
    if name == "phoenix":
        import final_phoenix as module

        module.STATE_FILE = STATE_PATHS[name]
        bot = module.FinalPhoenix()
        settings = phoenix_settings(module)
        account, start, save = bot.account, bot.run, bot.save_state
    else:
        if name == "final":
            from binance_cascade_hunter_dynamic_v2_paper import V2PaperHunter as Hunter
        else:
            from cascade_final3_paper import Final3PaperHunter as Hunter
        bot = Hunter(state_path=STATE_PATHS[name])
        account, start, save = bot.exchange, bot.start, bot.save
        settings = bot.settings()
    archive = FillArchive(archive_path(name))
    health = {}
    if name == "phoenix":
        observe_phoenix(bot, module, health)
    ready = False
    market_client = bot.exchange
    original_load = market_client.load_markets

    async def observed_load(*args, **kwargs):
        nonlocal ready
        result = await original_load(*args, **kwargs)
        ready = True
        return result

    market_client.load_markets = observed_load
    archive_error = None
    original_order = account.create_order

    def sync_archive():
        nonlocal archive_error
        try:
            archive.sync(account.trades)
            archive_error = None
        except Exception as exc:
            archive_error = str(exc)
            log.error("Execution archive update failed; saved state retained: %s", exc)

    async def archived_order(*args, **kwargs):
        fill = await original_order(*args, **kwargs)
        if name == "phoenix":
            closing = fill.get("reduceOnly", False)
            fill["exit_reason" if closing else "entry_reason"] = fill.get("reason")
            if closing:
                fill["partial_close"] = fill["symbol"] in account.positions
        # Capture every fill before the 150-item state window can discard it.
        sync_archive()
        return fill

    account.create_order = archived_order

    def publish(status):
        sync_archive()
        state = snapshot_state(name, bot, settings, status, health)
        state["archive_error"] = archive_error
        target = RUNTIME / "data" / f"{name}.snapshot.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, allow_nan=False), encoding="utf-8"
        )
        temporary.replace(target)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)

    async def report():
        while True:
            publish("running" if ready else "starting")
            await asyncio.sleep(3)

    tasks = []
    status = "stopped"
    try:
        save()
        publish("starting")
        tasks = [
            asyncio.create_task(start()),
            asyncio.create_task(report()),
            asyncio.create_task(stopped.wait()),
        ]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in tasks[:2]:
            if task in done:
                await task
    except Exception:
        status = "error"
        raise
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            save()
            publish(status)
        finally:
            archive.close()


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in NAMES:
        raise SystemExit("Choose final, final3 or phoenix")
    name = sys.argv[1]
    path = STATE_PATHS[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    (RUNTIME / "data").mkdir(parents=True, exist_ok=True)
    with (RUNTIME / "data" / f"{name}.lock").open("a") as process_lock, path.with_name(
        path.name + ".lock"
    ).open("a") as state_lock:
        try:
            for handle in (process_lock, state_lock):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(
                f"{LABELS[name]} is already running; no duplicate worker started"
            )
        asyncio.run(run_worker(name))


if __name__ == "__main__":
    main()
