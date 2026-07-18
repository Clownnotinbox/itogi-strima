@echo off
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: setup_pages_repo.bat https://github.com/USER/REPO.git
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_pages_repo.ps1" -RepoUrl "%~1"
pause
