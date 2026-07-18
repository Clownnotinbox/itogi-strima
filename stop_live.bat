@echo off
cd /d "%~dp0"
echo.
echo   Stopping Stream Quotes...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "foreach ($port in 8001,8000) { try { Invoke-WebRequest -UseBasicParsing http://127.0.0.1:$port/api/finalize-active -Method POST | Out-Null } catch {} }"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$root = [IO.Path]::GetFullPath('%~dp0'); $portPids = foreach ($port in 8001,8000) { Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.OwningProcess -gt 0 } | Select-Object -ExpandProperty OwningProcess -Unique }; foreach ($processId in ($portPids | Select-Object -Unique)) { Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue }; Get-CimInstance Win32_Process -Filter \"name='ffmpeg.exe'\" | Where-Object { $_.CommandLine -like ('*' + $root + 'data*audio*') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

echo   Stopped. You can close this window.
pause
