@echo off
rem One-click setup for Bookwyrm on Windows. Double-click this file.
rem It finds (or offers to install) Python, then hands over to installer\setup_bookwyrm.py,
rem which does the real work and writes setup-log.txt.
setlocal
title Bookwyrm Setup
cd /d "%~dp0"

rem Opened from inside the zip? Then we are in a temporary folder Windows will delete.
set "HERE=%~dp0"
if not "%HERE:\AppData\Local\Temp\=%"=="%HERE%" goto :inzip
if not exist "%~dp0installer\setup_bookwyrm.py" goto :incomplete

set "PYTHON="
set "PYEXE="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) and sys.maxsize > 2**32 else 1)" >nul 2>nul && set "PYTHON=py -3"
if not defined PYTHON python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) and sys.maxsize > 2**32 else 1)" >nul 2>nul && set "PYTHON=python"
if defined PYTHON goto :setup

echo Bookwyrm needs Python 3.11 or newer, which this PC does not have yet.
echo Python is free and safe; Bookwyrm is written in it.
echo.
choice /c YN /m "Install Python now"
if errorlevel 2 goto :manual
echo.
echo Installing Python. Windows may ask for permission...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
rem A new PATH only reaches windows opened later, so look where Python installs itself.
set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PYEXE%" goto :setup
set "PYEXE=%ProgramFiles%\Python312\python.exe"
if exist "%PYEXE%" goto :setup
set "PYEXE="
goto :manual

:setup
echo.
if defined PYEXE goto :withexe
%PYTHON% "%~dp0installer\setup_bookwyrm.py" %*
goto :after
:withexe
"%PYEXE%" "%~dp0installer\setup_bookwyrm.py" %*
:after
if errorlevel 1 goto :failed
echo.
echo All done. You can close this window.
timeout /t 8 >nul
exit /b 0

:manual
echo.
echo Python could not be installed automatically.
echo   1. Go to https://www.python.org/downloads/windows/ and download the latest
echo      "Windows installer (64-bit)".
echo   2. Run it and tick "Add python.exe to PATH" on its first screen.
echo   3. Double-click "Install Bookwyrm" again.
start "" "https://www.python.org/downloads/windows/"
goto :hold

:inzip
echo Bookwyrm is still inside its zip file.
echo Right-click the zip, choose "Extract All...", open the folder that creates,
echo and double-click "Install Bookwyrm" there.
goto :hold

:incomplete
echo Part of Bookwyrm is missing from this folder (the "installer" folder).
echo Unzip the whole download again and run "Install Bookwyrm" from there.
goto :hold

:failed
echo.
echo Setup did not finish. Please send the file "setup-log.txt" from this folder
echo to whoever gave you Bookwyrm.

:hold
echo.
pause
exit /b 1
