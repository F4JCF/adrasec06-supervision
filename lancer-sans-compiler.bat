@echo off
REM Lance le programme directement depuis les sources (pratique pour tester)
cd /d "%~dp0"
if not exist .venv (python -m venv .venv && call .venv\Scripts\activate.bat && pip install -r requirements.txt) else call .venv\Scripts\activate.bat
python run.py
