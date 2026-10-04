@echo off
rem ===========================================================================
rem  StreamLink - first-time installer (Windows)
rem
rem  Double-click this file. It:
rem    1. asks for administrator access (all-users Python, the Jackett service,
rem       firewall rules),
rem    2. finds a Python the background service can use, or installs one,
rem    3. opens the setup wizard (installer.py), which does everything else.
rem
rem  Keep this file plain ASCII with CRLF line endings (.gitattributes does the
rem  second): cmd.exe mis-parses labels in an LF-only batch file.
rem  See docs/INSTALLER.md.
rem ===========================================================================
setlocal EnableExtensions
title StreamLink Installer
cd /d "%~dp0"

if not exist "%~dp0installer.py" goto :not_extracted

rem fltmc needs an elevated token and exists on every Windows since Vista.
fltmc >nul 2>&1
if errorlevel 1 goto :elevate

echo ============================================================
echo                   StreamLink Installer
echo ============================================================
echo.

rem A usable Python is 3.9+, has Tk, and is NOT a per-user install: a venv
rem built from a per-user Python can only be run by that one account
rem (docs/GOTCHAS.md, "per-user Python"), and setup.py refuses to go on.
set "CHECK=import sys, tkinter; p = sys.executable.lower(); raise SystemExit(0 if sys.version_info >= (3, 9) and 'appdata' not in p and 'windowsapps' not in p else 1)"
set "PY="
call :scan
if defined PY goto :have_py

echo No suitable Python was found. Installing Python 3.12 for all users.
echo This takes a minute or two.
echo.
where winget >nul 2>&1
if errorlevel 1 goto :py_direct
winget install -e --id Python.Python.3.12 --scope machine --silent --accept-package-agreements --accept-source-agreements
call :scan
if defined PY goto :have_py

:py_direct
echo Downloading Python from python.org ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; $o=Join-Path $env:TEMP 'python-streamlink.exe'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile $o; Start-Process -Wait -FilePath $o -ArgumentList '/quiet','InstallAllUsers=1','PrependPath=1','Include_tcltk=1','Include_launcher=1'"
call :scan
if defined PY goto :have_py

echo.
echo [ERROR] Python could not be installed automatically.
echo Install Python 3.12 from https://www.python.org/downloads/ and, on the
echo installer's first screen, choose "Customize installation" and tick
echo "Install Python for all users". Then run install.bat again.
echo.
pause
exit /b 1

:have_py
echo Using Python: %PY%
echo Opening the setup wizard. This window stays open behind it.
echo.
rem One parenthesised block, so cmd has read it all before the wizard runs:
rem the wizard may replace this very file (a ZIP folder becomes a git clone),
rem and cmd would otherwise resume reading a changed file mid-way.
(
    %PY% "%~dp0installer.py"
    if errorlevel 1 pause
    exit /b
)

:elevate
echo Asking Windows for administrator access. Click Yes on the prompt.
set "SL_SELF=%~f0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "try { Start-Process -FilePath $env:SL_SELF -Verb RunAs -ErrorAction Stop } catch { exit 1 }"
if not errorlevel 1 exit /b 0
echo.
echo Administrator access was not given, so nothing was installed.
echo Run install.bat again and click Yes.
echo.
pause
exit /b 1

:not_extracted
echo.
echo install.bat is being run from inside the ZIP file.
echo Right-click the ZIP, choose "Extract All", then open the extracted
echo folder and double-click install.bat there.
echo.
pause
exit /b 1

rem -- Set PY to the first usable Python: all-users folders, then PATH. --------
:scan
for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PY call :try_py "%%~fD\python.exe"
if not defined PY call :try_py py -3
if not defined PY call :try_py python
goto :eof

:try_py
%* -c "%CHECK%" >nul 2>&1
if not errorlevel 1 set PY=%*
goto :eof
