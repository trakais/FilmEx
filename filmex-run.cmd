@echo off
REM FilmEx launcher for Windows.
setlocal
cd /d "%~dp0"

if "%FILMEX_PORT%"=="" set FILMEX_PORT=8765
set URL=http://127.0.0.1:%FILMEX_PORT%

start "" "%URL%"
uv run uvicorn filmex.main:app --host 127.0.0.1 --port %FILMEX_PORT% %*
