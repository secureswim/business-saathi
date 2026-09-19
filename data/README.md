Generated databases live here.

    python scripts/generate.py
    python scripts/recompute_patterns.py
    copy data\saathi.db data\saathi.seed.db

saathi.db      the working ledger
saathi.seed.db the known state the reset button restores

Both are gitignored: ~80MB, and they rebuild from a seed constant in under
five seconds.
