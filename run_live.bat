@echo off
rem Live mode: watches Twitch channels while this window is open.
cd /d "%~dp0"

set "KRYL_PYTHON=py -3.11"
if exist ".venv\Scripts\python.exe" set "KRYL_PYTHON=.venv\Scripts\python.exe"

set KRYL_LIVE_CHUNK_SECONDS=60
set KRYL_LIVE_MAX_SECONDS=0
set KRYL_LIVE_QUOTE_LIMIT=30
set KRYL_LIVE_MIN_QUOTES=3
set KRYL_LIVE_QUOTES_PER_HOUR=2.8
set KRYL_LIVE_DYNAMIC_KEEP_THRESHOLD=72
set KRYL_LIVE_MAX_WORDS=38
set KRYL_LIVE_USE_LLM=1
set KRYL_LIVE_SUMMARY_SECONDS=7200
set KRYL_LIVE_QUOTE_INTERVAL_SECONDS=7200
set KRYL_LIVE_SITE_PUBLISH_SECONDS=7200
set KRYL_LIVE_RECONNECT_GRACE_SECONDS=900
set KRYL_LIVE_RECONNECT_DELAY_SECONDS=20
set KRYL_PAGES_AUTO_PUSH=0
if "%KRYL_PORT%"=="" set KRYL_PORT=8001

echo.
echo   Итоги стрима live mode
echo   Open http://127.0.0.1:%KRYL_PORT%
echo   Close this window to stop the local bot.
echo   Quotes/site updates: every 2 hours and at stream end
echo   Interim summaries: every 2 hours in data\summaries
echo   Short Twitch drops: reconnect for up to 15 minutes
echo   Transcripts are kept in data\transcripts for later review
echo.

start "" "http://127.0.0.1:%KRYL_PORT%"
%KRYL_PYTHON% -m uvicorn app:app --host 127.0.0.1 --port %KRYL_PORT%
pause
