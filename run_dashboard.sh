#!/usr/bin/env bash
set -euo pipefail
repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$repo_dir"
python3 -c 'import sys; assert sys.version_info >= (3,11), "Python 3.11+ required; use Ubuntu in Termux"'
python3 - <<'PY'
import socket
with socket.socket() as sock:
    # Match aiohttp: allow a stopped listener's TCP TIME_WAIT sockets to expire.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(('127.0.0.1',8787))
    except OSError:
        raise SystemExit('Port 8787 is busy. Stop the old dashboard with Ctrl+C before starting all three bots.')
PY
runtime_record="$repo_dir/.runtime/dashboard-location"
fresh=0
server_args=()
for argument in "$@"; do
 if [[ "$argument" == "--fresh" ]]; then
  fresh=1
 else
  server_args+=("$argument")
 fi
done
if [[ "$fresh" == 1 ]]; then
 CASCADE_RUNTIME_DIR="$(python3 - <<'PYSESSION'
import tempfile
from datetime import datetime, timezone
from pathlib import Path
sessions = Path('.runtime/sessions').resolve()
sessions.mkdir(parents=True, exist_ok=True)
print(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime('fresh-%Y%m%d-%H%M%S-'), dir=sessions))
PYSESSION
)"
 export CASCADE_FINAL_STATE="$CASCADE_RUNTIME_DIR/data/final.state.json"
 export CASCADE_FINAL3_STATE="$CASCADE_RUNTIME_DIR/data/final3.state.json"
 export CASCADE_PHOENIX_STATE="$CASCADE_RUNTIME_DIR/data/phoenix.state.json"
 printf 'NEW SESSION: all three bots start with 100 USDT and zero trades. Previous sessions are preserved.\n'
elif [[ -z "${CASCADE_RUNTIME_DIR:-}" && -f "$runtime_record" ]]; then
 IFS= read -r CASCADE_RUNTIME_DIR < "$runtime_record"
fi
export CASCADE_RUNTIME_DIR="${CASCADE_RUNTIME_DIR:-$repo_dir/.runtime/dashboard}"
if [[ -z "${CASCADE_FINAL3_DIR:-}" ]]; then
 if [[ -d "$repo_dir/../Final-3/.git" ]]; then
  CASCADE_FINAL3_DIR="$repo_dir/../Final-3"
 else
  CASCADE_FINAL3_DIR="$repo_dir/.runtime/repos/Final-3"
 fi
fi
export CASCADE_FINAL3_DIR
if [[ -z "${CASCADE_FINAL_DIR:-}" ]]; then
 if [[ -d "$repo_dir/../Final/.git" ]]; then
  CASCADE_FINAL_DIR="$repo_dir/../Final"
 elif [[ -d "$CASCADE_FINAL3_DIR/.runtime/repos/Final/.git" ]]; then
  CASCADE_FINAL_DIR="$CASCADE_FINAL3_DIR/.runtime/repos/Final"
 else
  CASCADE_FINAL_DIR="$repo_dir/.runtime/repos/Final"
 fi
fi
export CASCADE_FINAL_DIR
update_source() {
 local source_dir="$1" repo_name="$2"
 if [[ ! -d "$source_dir/.git" ]]; then
  if [[ -e "$source_dir" ]]; then
   echo "Source exists but is not a Git checkout: $source_dir" >&2; exit 1
  fi
  mkdir -p -- "$(dirname -- "$source_dir")"
  git clone --depth 1 --branch main "https://github.com/khaledhasneen1993/$repo_name.git" "$source_dir"
 else
  if [[ -n "$(git -C "$source_dir" status --porcelain --untracked-files=no)" ]]; then
   echo "Source has local edits; preserve them before updating: $source_dir" >&2; exit 1
  fi
  git -C "$source_dir" pull --ff-only origin main
 fi
}
update_source "$CASCADE_FINAL3_DIR" Final-3
update_source "$CASCADE_FINAL_DIR" Final
if [[ ! -x .venv-dashboard/bin/python ]]; then
 python3 -m venv .venv-dashboard
fi
expected_hash="$(sha256sum dashboard/requirements.txt | cut -d ' ' -f 1)"
installed_hash="$(cat .venv-dashboard/requirements.sha256 2>/dev/null || true)"
if [[ "$installed_hash" != "$expected_hash" ]]; then
 .venv-dashboard/bin/python -m pip install -r dashboard/requirements.txt
 printf '%s\n' "$expected_hash" > .venv-dashboard/requirements.sha256
fi
.venv-dashboard/bin/python dashboard/prepare.py
mkdir -p .runtime
printf '%s\n' "$CASCADE_RUNTIME_DIR" > "$runtime_record"
printf '\nFinal + Final-3 + Phoenix dashboard: http://127.0.0.1:8787\nCtrl+C stops all three bots and saves their independent accounts.\n\n'
exec .venv-dashboard/bin/python -u dashboard/server.py "${server_args[@]}"
