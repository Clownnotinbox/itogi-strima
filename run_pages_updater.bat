@echo off
cd /d "%~dp0"
echo.
echo   Итоги стрима: обновление GitHub Pages каждые 2 часа
echo   Оставь это окно открытым вместе с live-ботом.
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_pages.ps1" -Loop -IntervalSeconds 7200
pause
