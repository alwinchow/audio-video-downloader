@echo off
title Audio Downloader - keep this window open
cd /d "%~dp0"
echo Starting the Audio Downloader server...
echo.
echo Once it says "Running on http://127.0.0.1:5000", open that address
echo in your browser. Keep THIS window open while you use it.
echo Close this window (or press Ctrl+C) to stop the server.
echo.
start "" http://127.0.0.1:5000/
py server.py
echo.
echo Server stopped. Press any key to close.
pause >nul
