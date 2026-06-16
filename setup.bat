@echo off
setlocal enableextensions
title Tokey setup

echo ============================================
echo    Tokey setup
echo ============================================
echo.

REM --- Find a Python launcher: prefer the py launcher, fall back to python ---
set "PYCMD="
where py     >nul 2>nul && set "PYCMD=py"
if defined PYCMD goto py_ok
where python >nul 2>nul && set "PYCMD=python"
:py_ok
if not defined PYCMD goto no_python

REM --- Require Python 3.11+ (double quotes keep cmd from eating the >= ) ---
%PYCMD% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)"
if errorlevel 1 goto old_python

echo Using Python:
%PYCMD% --version
echo.
echo Installing tokey. The first time this can take a minute...
echo.

%PYCMD% -m pip install "%~dp0."
if errorlevel 1 goto install_failed

REM Tokey is now copied into Python, so this folder is no longer needed. Drop a
REM standalone launcher on the Desktop so it can still be started after the
REM folder is deleted (PowerShell resolves the real Desktop, OneDrive included).
set "LAUNCHER_OK="
powershell -NoProfile -ExecutionPolicy Bypass -Command "Copy-Item -LiteralPath '%~dp0run-tokey.bat' -Destination (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Tokey.bat') -Force" && set "LAUNCHER_OK=1"

echo.
echo ============================================
echo    Done^! Tokey is installed.
echo.
if defined LAUNCHER_OK echo    A "Tokey" launcher is on your Desktop -- double-click it to start.
if defined LAUNCHER_OK echo    You can now delete this folder if you like.
if not defined LAUNCHER_OK echo    To start it, double-click:  run-tokey.bat  in this folder.
echo ============================================
echo.
pause
exit /b 0

:no_python
echo [ X ]  Python was not found on this computer.
echo.
echo        Tokey needs Python 3.11 or newer.
echo        1. Get it from:  https://www.python.org/downloads/windows/
echo        2. On the FIRST installer screen, tick
echo           "Add python.exe to PATH".
echo        3. Then run this setup.bat again.
echo.
pause
exit /b 1

:old_python
echo [ X ]  Your Python is older than 3.11:
%PYCMD% --version
echo.
echo        Please install Python 3.11 or newer from:
echo        https://www.python.org/downloads/windows/
echo        then run this setup.bat again.
echo.
pause
exit /b 1

:install_failed
echo.
echo [ X ]  Install failed. Scroll up to read the error message.
echo.
pause
exit /b 1
