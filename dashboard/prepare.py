"""Preflight accounts without resetting them; the approved upstream state migration is used."""

import fcntl
import importlib.util
import json
import sys
from paths import NAMES, SOURCES, STATE_PATHS, LABELS, save_selection


def prepare():
    entries = {
        "final": "binance_cascade_hunter_dynamic_v2_paper.py",
        "final3": "cascade_final3_paper.py",
        "phoenix": "final_phoenix.py",
    }
    for name, entry in entries.items():
        if not (SOURCES[name] / entry).is_file():
            raise SystemExit(
                f"Missing {LABELS[name]} entry point: {SOURCES[name] / entry}"
            )
    for name in NAMES:
        path = STATE_PATHS[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise SystemExit(
                    f"{LABELS[name]} is already running on {path}. Stop it first with Ctrl+C."
                )
            if name == "final3" and path.exists():
                sys.path.insert(0, str(SOURCES[name]))
                try:
                    migration_path = SOURCES[name] / "migrate_state.py"
                    if not migration_path.exists():
                        migration_path = SOURCES[name] / "migrate_idle_state.py"
                    spec = importlib.util.spec_from_file_location(
                        "final3_state_migration", migration_path
                    )
                    migration = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(migration)
                    backup = migration.migrate(path)
                    if backup:
                        print(
                            "Final-3 compatible state metadata updated with backup:",
                            backup,
                        )
                finally:
                    sys.path.pop(0)
            state = json.loads(path.read_text()) if path.exists() else {}
            print(f"{LABELS[name]} account: {path}")
            print(
                "  Resuming balance:",
                state.get("balance_usdt", state.get("balance", 100)),
                "| fills available:",
                len(state.get("trades", [])),
                "| open positions:",
                len(state.get("account_positions", {})),
            )
    save_selection()


if __name__ == "__main__":
    prepare()
