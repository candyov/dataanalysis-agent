@echo off
cd /d %~dp0
set PYTHONPATH=src
pip install aiosqlite>nul 2>&1
echo Starting Data Intelligence Agent on http://0.0.0.0:8010
uvicorn dia.main:app --host 0.0.0.0 --port 8010 --reload
