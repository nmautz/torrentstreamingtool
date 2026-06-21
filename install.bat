@echo off
REM ===========================================================================
REM  StreamLink - First-Time Installer (Windows)
REM
REM  Double-click this file. It will:
REM    1. ask for Administrator access (needed for Python all-users, the
REM       Jackett service, firewall rules and binding ports 80/443),
REM    2. install Python 3.12 (all users) if a suitable one isn't present,
REM    3. launch the graphical setup wizard (installer.py), which drives
REM       setup.py and then offers to start StreamLink.
REM
REM  No prior install of Python, VLC, qBittorrent, Jackett or Mullvad is
REM  required - the wizard installs everything it can via winget.
REM ===========================================================================
setlocal EnableExtensions EnableDelayedExpansion
title StreamLink Installer

REM ── Self-elevate to Administrator ──────────────────────────────────────────
>nul 2>&1 net session
if %errorlevel% neq 0 (
    echo Requesting administrator access ^(accept the prompt^)...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"

echo ============================================================
echo            StreamLink - First-Time Installer
echo ============================================================
echo.

REM ── Locate a usable Python (3.9+) ──────────────────────────────────────────
set "PY="
call :try_py "py -3"
if not defined PY call :try_py "python"

if not defined PY (
    echo Python 3.9+ was not found - installing Python ^(all users^)...
    echo.
    where winget >nul 2>&1
    if !errorlevel! equ 0 (
        winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
    ) else (
        echo winget is unavailable - downloading the official Python installer...
        powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $u='https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe'; $o=Join-Path $env:TEMP 'python-streamlink.exe'; Invoke-WebRequest -UseBasicParsing -Uri $u -OutFile $o; Start-Process -Wait -FilePath $o -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_tcltk=1','Include_launcher=1'"
    )
    REM Re-detect after install (the py launcher lands in %WINDIR%, on PATH now).
    call :try_py "py -3"
    if not defined PY call :try_py "python"
)

if not defined PY (
    echo.
    echo [ERROR] Could not find or install Python 3.9+.
    echo Install it manually from https://www.python.org/downloads/ ^(tick
    echo "Add python.exe to PATH"^) and run this installer again.
    echo.
    pause
    exit /b 1
)

echo Using Python launcher: %PY%
echo Opening the setup wizard...
echo.

%PY% "%~dp0installer.py"
set "RC=%errorlevel%"

if not "%RC%"=="0" (
    echo.
    echo The installer window closed with exit code %RC%.
    pause
)
exit /b %RC%

REM ── Subroutine: set PY if "%~1" is a working Python >= 3.9 ──────────────────
:try_py
%~1 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if %errorlevel% equ 0 set "PY=%~1"
goto :eof
