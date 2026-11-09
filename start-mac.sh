#!/usr/bin/env sh
# Starter Bokhylla på denne Macen: lager et virtuelt Python-miljø første gang,
# holder pakkene oppdatert, og åpner appen i nettleseren.
set -eu
cd "$(dirname "$0")"

[ -f .env ] || cp .env.example .env

if [ ! -x .venv/bin/python ]; then
  echo "Oppretter virtuelt Python-miljø ..."
  python3 -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt

set -a
. ./.env
set +a
PORT="${APP_PORT:-8080}"

echo "Bokhylla kjører på http://localhost:$PORT (stopp med Ctrl+C)"
(sleep 2 && open "http://localhost:$PORT") >/dev/null 2>&1 &
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
