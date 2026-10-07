@echo off
setlocal
cd /d "%~dp0"
title Recap English to Myanmar - Narrator - Edge TTS - Smart Sync

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\launch.ps1"
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo ============================================================
  echo   APP CLOSED WITH ERROR - EXIT CODE %EXITCODE%
  echo ============================================================
  echo.
  pause
)
exit /b %EXITCODE%
