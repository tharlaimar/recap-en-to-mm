@echo off
setlocal
cd /d "%~dp0"
title Install Recap English to Myanmar requirements

set "PY=python"
where py >nul 2>nul && set "PY=py -3.12"

echo [1/4] Updating pip...
%PY% -m pip install --upgrade pip || goto :fail
echo [2/4] Installing Python packages from requirements.txt...
%PY% -m pip install -r requirements.txt || goto :fail
echo [3/4] Installing Chromium for the Myanmar text renderer...
%PY% -m playwright install chromium || goto :fail
echo [4/4] Checking FFmpeg...
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\get_ffmpeg.ps1" || goto :ffmpeg_fail

echo.
echo Done. Put your Gemini key in .env (copy .env.example) and run RUN.bat.
pause
exit /b 0

:ffmpeg_fail
echo.
echo FFmpeg could not be downloaded automatically.
echo Download it from https://www.gyan.dev/ffmpeg/builds/ (ffmpeg-release-essentials.zip)
echo and copy ffmpeg.exe and ffprobe.exe from its bin folder into the "ffmpeg" folder next to RUN.bat.
pause
exit /b 1

:fail
echo.
echo FAILED - check the messages above. Python 3.12 (64-bit) and internet are needed.
pause
exit /b 1
