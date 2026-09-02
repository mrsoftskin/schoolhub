@echo off
rem Command Center installer - double-click this.
rem It just runs install.ps1 (PowerShell does the real work: installs a signed
rem Python, sets up the app, and downloads the AI libraries).
title Command Center Setup
echo Starting Command Center setup...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo Setup did not finish. See the messages above, or send data\logs to whoever gave you this.
  pause
)
