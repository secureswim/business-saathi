#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
if [ ! -f data/saathi.db ]; then
  echo "no database yet; generating..."
  python3 scripts/generate.py
  python3 scripts/recompute_patterns.py
  cp data/saathi.db data/saathi.seed.db
fi
[ -f data/saathi.seed.db ] || cp data/saathi.db data/saathi.seed.db
exec python3 -m uvicorn backend.api.main:app --host 127.0.0.1 --port "${SAATHI_PORT:-8000}"
