#!/bin/sh
set -e

cd "$(dirname "$0")"
if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

exec python3 server.py
