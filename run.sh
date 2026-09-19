#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# The generator refuses to write a database whose demo story does not hold --
# see the SystemExit at the end of scripts/generate.py. That is right locally:
# you want to know before a rehearsal, not during one. It is wrong here: the
# database has already been written by the time that check runs, so killing
# the start command would leave a dead service over a data-quality warning.
# So the warning is surfaced and the service still boots.
if [ ! -f data/saathi.db ]; then
  echo "no database yet; generating..."
  if ! python3 scripts/generate.py; then
    echo ""
    echo "  !! The generated data does not tell the demo story (see above)."
    echo "  !! Serving it anyway. Check /ops before showing this to anyone."
    echo ""
  fi
  python3 scripts/recompute_patterns.py
  cp data/saathi.db data/saathi.seed.db
fi
[ -f data/saathi.seed.db ] || cp data/saathi.db data/saathi.seed.db
exec python3 -m uvicorn backend.api.main:app --host "${SAATHI_HOST:-127.0.0.1}" --port "${PORT:-${SAATHI_PORT:-8000}}"
