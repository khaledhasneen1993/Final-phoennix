"""Three isolated sources and state files; explicit paths override preserved accounts."""

import hashlib
import json
import os
from pathlib import Path

ASSETS = Path(__file__).resolve().parent
ROOT = ASSETS.parent
SELECTION_FILE = ROOT / ".runtime/dashboard-accounts.json"


def load_selection():
    if not SELECTION_FILE.exists():
        return {}
    data = json.loads(SELECTION_FILE.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("states"), dict):
        raise ValueError(
            "Invalid dashboard account selection; refusing to fall back to old accounts"
        )
    required = ("runtime",)
    paths = [data[k] for k in required] + [
        data["states"][n] for n in ("final", "final3", "phoenix")
    ]
    if not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
        raise ValueError("Saved dashboard paths must be absolute")
    return data


SAVED = load_selection()
RUNTIME = (
    Path(
        os.environ.get(
            "CASCADE_RUNTIME_DIR",
            SAVED.get("runtime", str(ROOT / ".runtime/dashboard")),
        )
    )
    .expanduser()
    .resolve()
)
SAVED_STATES = SAVED.get("states", {}) if str(RUNTIME) == SAVED.get("runtime") else {}
SOURCES = {
    "final": Path(
        os.environ.get("CASCADE_FINAL_DIR", str(ROOT / ".runtime/repos/Final"))
    )
    .expanduser()
    .resolve(),
    "final3": Path(os.environ.get("CASCADE_FINAL3_DIR", str(ROOT.parent / "Final-3")))
    .expanduser()
    .resolve(),
    "phoenix": ROOT,
}
NAMES = ("final", "final3", "phoenix")
LABELS = {"final": "Final", "final3": "Final-3", "phoenix": "Phoenix"}


STATE_PATHS = {
    "final": Path(
        os.environ.get(
            "CASCADE_FINAL_STATE",
            SAVED_STATES.get("final", str(RUNTIME / "data/final.state.json")),
        )
    )
    .expanduser()
    .resolve(),
    "final3": Path(
        os.environ.get(
            "CASCADE_FINAL3_STATE",
            SAVED_STATES.get("final3", str(RUNTIME / "data/final3.state.json")),
        )
    )
    .expanduser()
    .resolve(),
    "phoenix": Path(
        os.environ.get(
            "CASCADE_PHOENIX_STATE",
            SAVED_STATES.get("phoenix", str(RUNTIME / "data/phoenix.state.json")),
        )
    )
    .expanduser()
    .resolve(),
}
if len(set(STATE_PATHS.values())) != len(NAMES):
    raise ValueError("Each bot must use its own state file")


def archive_path(name):
    key = hashlib.sha256(str(STATE_PATHS[name]).encode()).hexdigest()[:16]
    return RUNTIME / "data" / f"{name}-{key}.fills.sqlite"


def save_selection():
    SELECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "runtime": str(RUNTIME),
        "states": {name: str(path) for name, path in STATE_PATHS.items()},
    }
    temporary = SELECTION_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(SELECTION_FILE)
