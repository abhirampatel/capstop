#!/usr/bin/env bash
# One-command setup + run for capstop (tested on Linux only; macOS and Windows UNTESTED).
#   ./run.sh                -> capstop serve (needs CAPSTOP_UPSTREAM, default https://api.openai.com)
#   ./run.sh fake           -> no-key demo: capstop fake on :9999 + capstop serve on :8787 pointed at it
#   ./run.sh setup          -> only create/refresh the venv
# Extra args after the mode are passed to `capstop serve`.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${CAPSTOP_VENV:-$HERE/.venv}"
PY="${PYTHON:-python3}"
LOCK="$HERE/requirements.lock"
STAMP="$VENV/.lock.sha256"

die() { echo "run.sh: error: $*" >&2; exit 1; }

command -v "$PY" >/dev/null 2>&1 || die "'$PY' not found. Install Python >= 3.11 or set PYTHON=/path/to/python3.11+"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Python >= 3.11 required, found $("$PY" -V 2>&1)"
"$PY" -c 'import venv, ensurepip' 2>/dev/null || die "python venv/ensurepip missing (Debian/Ubuntu: apt install python3-venv)"
[ -f "$LOCK" ] || die "missing $LOCK"

setup() {
  if [ ! -x "$VENV/bin/python" ]; then
    echo "run.sh: creating venv at $VENV" >&2
    "$PY" -m venv "$VENV" || die "venv creation failed"
  fi
  local want have=""
  want="$(cat "$LOCK" "$HERE/pyproject.toml" "$HERE"/capstop/*.py "$HERE/capstop/prices.json" | sha256sum | cut -d' ' -f1)"
  [ -f "$STAMP" ] && have="$(cat "$STAMP")"
  if [ "$want" = "$have" ] && "$VENV/bin/capstop" --help >/dev/null 2>&1; then
    echo "run.sh: venv up to date" >&2
    return
  fi
  echo "run.sh: installing pinned deps (hash-checked)" >&2
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check --require-hashes -r "$LOCK" \
    || die "pip install of requirements.lock failed"
  "$VENV/bin/python" -m pip install -q --disable-pip-version-check --no-deps --no-build-isolation "$HERE" \
    || die "pip install of the project failed"
  "$VENV/bin/capstop" --help >/dev/null || die "capstop entry point not working after install"
  echo "$want" > "$STAMP"
}

mode="${1:-serve}"; [ $# -gt 0 ] && shift
case "$mode" in
  setup) setup ;;
  serve)
    setup
    exec "$VENV/bin/capstop" serve --upstream "${CAPSTOP_UPSTREAM:-https://api.openai.com}" "$@" ;;
  fake)
    setup
    FAKE_PORT="${CAPSTOP_FAKE_PORT:-9999}"
    "$VENV/bin/capstop" fake --port "$FAKE_PORT" &
    FAKE_PID=$!
    trap 'kill "$FAKE_PID" 2>/dev/null || true; wait "$FAKE_PID" 2>/dev/null || true' EXIT INT TERM
    for _ in $(seq 1 50); do
      kill -0 "$FAKE_PID" 2>/dev/null || die "capstop fake exited (port $FAKE_PORT in use?)"
      (exec 3<>"/dev/tcp/127.0.0.1/$FAKE_PORT") 2>/dev/null && break
      sleep 0.1
    done
    echo "run.sh: fake upstream on 127.0.0.1:$FAKE_PORT (pid $FAKE_PID)" >&2
    "$VENV/bin/capstop" serve --upstream "http://127.0.0.1:$FAKE_PORT" "$@" ;;
  -h|--help|help) sed -n '2,7p' "$0" ;;
  *) die "unknown mode '$mode' (use: serve | fake | setup)" ;;
esac
