@echo off
rem Build dist\Bookwyrm\Bookwyrm.exe on Windows. Usage: packaging\build.bat [--zip]
rem Needs Python 3.11 or newer from python.org (tick "Add python.exe to PATH" when installing).
cd /d "%~dp0\.."
if not exist .venv\Scripts\python.exe (
    py -3 -m venv .venv || python -m venv .venv || goto :nopython
)
.venv\Scripts\python -m pip install -q -e . || exit /b 1
.venv\Scripts\python packaging\build.py %* || exit /b 1
exit /b 0
:nopython
echo Python was not found. Install Python 3.11 or newer from https://www.python.org/downloads/
exit /b 1
