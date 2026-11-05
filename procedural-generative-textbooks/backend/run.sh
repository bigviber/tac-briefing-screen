#!/usr/bin/env bash
# Run the Axiom backend: create/use .venv, install requirements, start uvicorn on 127.0.0.1.
#   ./run.sh                 # http://127.0.0.1:8000
#   AXIOM_PORT=8010 ./run.sh
#   AXIOM_RELOAD=1 ./run.sh  # auto-reload for development
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "creating .venv with $("$PYTHON" --version)"
  "$PYTHON" -m venv .venv
fi

STAMP=.venv/.requirements.sha
WANT="$(sha256sum requirements.txt | cut -d' ' -f1)"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$WANT" ]; then
  .venv/bin/python -m pip install --upgrade pip >/dev/null
  .venv/bin/python -m pip install -r requirements.txt
  echo "$WANT" > "$STAMP"
fi

PORT="${AXIOM_PORT:-8000}"
RELOAD_FLAG=()
if [ "${AXIOM_RELOAD:-0}" = "1" ]; then
  RELOAD_FLAG=(--reload)
fi

echo "Axiom backend on http://127.0.0.1:${PORT}  (API: /api/v1, docs: /docs)"
exec .venv/bin/python -m uvicorn main:create_app --factory --host 127.0.0.1 --port "$PORT" "${RELOAD_FLAG[@]}"
