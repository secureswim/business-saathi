@echo off
cd /d "%~dp0"
set "PYTHON=python"
if exist ".venv\Scripts\python.exe" set "PYTHON=.venv\Scripts\python.exe"
if not exist data\saathi.db (
  echo no database yet; generating...
  "%PYTHON%" scripts\generate.py
  "%PYTHON%" scripts\recompute_patterns.py
  copy data\saathi.db data\saathi.seed.db
)
if not exist data\saathi.seed.db copy data\saathi.db data\saathi.seed.db
"%PYTHON%" -m uvicorn backend.api.main:app --host 127.0.0.1 --port 8000
