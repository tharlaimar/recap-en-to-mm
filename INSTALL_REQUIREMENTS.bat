@echo off
setlocal
cd /d "%~dp0"
title Install Recap English to Myanmar requirements

set "PY=python"
where py >nul 2>nul && set "PY=py -3.12"

echo [1/3] Updating pip...
%PY% -m pip install --upgrade pip || goto :fail
echo [2/3] Installing Python packages from requirements.txt...
%PY% -m pip install -r requirements.txt || goto :fail
echo [3/3] Installing Chromium for the Myanmar text renderer...
%PY% -m playwright install chromium || goto :fail

echo.
echo Done.
echo Also needed: FFmpeg (ffmpeg.exe + ffprobe.exe on PATH or in C:\ffmpeg\bin).
echo Then put your Gemini key in .env (copy .env.example) and run RUN.bat.
pause
exit /b 0

:fail
echo.
echo FAILED - check the messages above. Python 3.12 (64-bit) and internet are needed.
pause
exit /b 1
